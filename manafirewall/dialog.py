#  dialog.py
# -*- coding: utf-8 -*-

'''
Python manafirewall.dialog contains main manafirewall window

License: LGPLv2+

Author:  Angelo Naselli <anaselli@linux.it>

@package manafirewall
'''


import manatools.ui.common as common
import manatools.ui.basedialog as basedialog
import manatools.services as mnservices
import yui

from dbus.exceptions import DBusException

from firewall import config
from firewall import client
from firewall import functions
from firewall.core.base import DEFAULT_ZONE_TARGET, REJECT_TYPES, \
                               ZONE_SOURCE_IPSET_TYPES
from firewall.core.ipset import IPSET_MAXNAMELEN
from firewall.core.helper import HELPER_MAXNAMELEN
from firewall.core.io.zone import Zone
from firewall.core.io.service import Service
from firewall.core.io.icmptype import IcmpType
from firewall.core.io.ipset import IPSet
from firewall.core.io.helper import Helper
from firewall.core import rich
from firewall.core.fw_nm import nm_is_imported, nm_get_dbus_interface, \
                                nm_get_connections, nm_get_zone_of_connection, \
                                nm_set_zone_of_connection
from firewall import errors
from firewall.errors import FirewallError
import gettext
import time

from manafirewall.version import __project_version__ as VERSION
from manafirewall.version import __project_name__ as PROJECT

from queue import SimpleQueue, Empty

import manafirewall.zoneBaseDialog as zoneBaseDialog
import manafirewall.serviceBaseDialog as serviceBaseDialog

def TimeFunction(func):
    """
    This decorator prints execution time
    """
    def newFunc(*args, **kwargs):
        t_start = time.time()
        rc = func(*args, **kwargs)
        t_end = time.time()
        name = func.__name__
        print("%d: %s took %.2f sec"%(t_start, name, t_end - t_start))
        return rc

    newFunc.__name__ = func.__name__
    newFunc.__doc__ = func.__doc__
    newFunc.__dict__.update(func.__dict__)
    return newFunc

class ManaWallDialog(basedialog.BaseDialog):
  '''
  manafirewall main dialog
  '''
  def __init__(self):
    gettext.install('manafirewall', localedir='/usr/share/locale', names=('ngettext',))
    basedialog.BaseDialog.__init__(self, _("Manatools - firewalld configurator"), "", basedialog.DialogType.POPUP, 80, 10)
    self._application_name = _("{} - ManaTools firewalld configurator").format(PROJECT)

    # most used text
    self.connected_label = _("Connection to firewalld established.")
    self.trying_to_connect_label = \
        _("Trying to connect to firewalld, waiting...")
    self.failed_to_connect_label = \
        _("Failed to connect to firewalld. Please make sure that the "
          "service has been started correctly and try again.")
    self.changes_applied_label = _("Changes applied.")
    self.used_by_label = _("Used by network connection '%s'")
    self.default_zone_used_by_label = _("Default zone used by network "
                                        "connection '%s'")
    self.enabled = _("enabled")
    self.disabled = _("disabled")
    self.connection_lost = False
    self.log_denied = ""
    self.automatic_helpers = ""
    self.active_zones = { }
    self.runtime_view = True
    self.replacePointWidgetsAndCallbacks = []

    self.fwEventQueue = SimpleQueue()


  def UIlayout(self, layout):
    '''
    layout implementation called in base class to setup UI
    '''

    # Let's test a Menu widget
    self.file_menu = self.factory.createMenuButton(self.factory.createLeft(layout), _("&File"))
    qm = yui.YMenuItem(_("&Quit"))
    self.file_menu.addItem(qm)
    self.file_menu.rebuildMenuTree()
    sendObjOnEvent=True
    self.eventManager.addMenuEvent(qm, self.onQuitEvent, sendObjOnEvent)

    # _______
    #|   |   |
    #
    cols = self.factory.createHBox(layout)
    col1 = self.factory.createVBox(cols)
    col2 = self.factory.createVBox(cols)

    # Column 1
    self.activeBindingsTree = self.factory.createTree(col1, _("Active bindings"))
    col1.setWeight(yui.YD_HORIZ, 30)
    changeBindingsButton = self.factory.createPushButton(col1, _("&Change binding") )
    self.eventManager.addWidgetEvent(changeBindingsButton, self.onChangeBinding, sendObjOnEvent)
    #### editFrameBox contains button to modify zones (add, remove, edit, load defaults)
    self.editFrameBox = self.factory.createFrame(col1, _("Edit zones"))
    hbox = self.factory.createHBox( self.editFrameBox )
    vbox1 = self.factory.createVBox(hbox)
    vbox2 = self.factory.createVBox(hbox)
    editFrameAddButton          = self.factory.createPushButton(vbox1, _("&Add") )
    self.eventManager.addWidgetEvent(editFrameAddButton, self.onEditFrameAddButtonEvent)
    editFrameEditButton         = self.factory.createPushButton(vbox1, _("&Edit") )
    self.eventManager.addWidgetEvent(editFrameEditButton, self.onEditFrameEditButtonEvent)
    editFrameRemoveButton       = self.factory.createPushButton(vbox2, _("&Remove") )
    self.eventManager.addWidgetEvent(editFrameRemoveButton, self.onEditFrameRemoveButtonEvent)
    editFrameLoadDefaultsButton = self.factory.createPushButton(vbox2, _("&Load default") )
    self.eventManager.addWidgetEvent(editFrameLoadDefaultsButton, self.onEditFrameLoadDefaultsButtonEvent)
    self.editFrameBox.setEnabled(False)

    # Column 2
    align = self.factory.createTop(col2) #self.factory.createLeft(col2)
    align = self.factory.createLeft(align)
    hbox = self.factory.createHBox(align)
    col2.setWeight(yui.YD_HORIZ, 80)

    #label = self.factory.createLabel(hbox, _("Configuration:"),False,False)
    self.views = {
            'runtime'   : {'title' : _("Runtime")},
            'permanent' : {'title' : _("Permanent")},
        }
    ordered_views = [ 'runtime', 'permanent' ]

    self.currentViewCombobox = self.factory.createComboBox(hbox,_("Configuration"))
    itemColl = yui.YItemCollection()

    for v in ordered_views:
      item = yui.YItem(self.views[v]['title'], False)
      show_item = 'runtime'
      if show_item == v :
          item.setSelected(True)
      # adding item to views to find the item selected
      self.views[v]['item'] = item
      itemColl.push_back(item)
      item.this.own(False)

    self.currentViewCombobox.addItems(itemColl)
    self.currentViewCombobox.setNotify(True)
    self.eventManager.addWidgetEvent(self.currentViewCombobox, self.onChangeView)

    # mainNotebook (configure combo box)
    # TODO icmp_types, helpers, direct_configurations, lockdown_whitelist
    self.configureViews = {
            'zones'    : {'title' : _("Zones")},
            'services' : {'title' : _("Services")},
            'ipsets'   : {'title' : _("IP Sets")},
    }
    ordered_configureViews = [ 'zones', 'services', 'ipsets' ]
    self.configureViewCombobox = self.factory.createComboBox(hbox,_("View"))
    itemColl = yui.YItemCollection()

    for v in ordered_configureViews:
      item = yui.YItem(self.configureViews[v]['title'], False)
      show_item = 'zones'
      if show_item == v :
          item.setSelected(True)
      # adding item to views to find the item selected
      self.configureViews[v]['item'] = item
      itemColl.push_back(item)
      item.this.own(False)

    self.configureViewCombobox.addItems(itemColl)
    self.configureViewCombobox.setNotify(True)
    self.eventManager.addWidgetEvent(self.configureViewCombobox, self.onConfigurationViewChanged)

    # selectedConfigurationCombo is filled if requested by selected configuration view (which zones, services ...)
    self.selectedConfigurationCombo = self.factory.createComboBox(hbox,"     ")
    # adding a dummy item to enlarge combobox
    item = yui.YItem("--------------------", False)
    item.this.own(False)
    self.selectedConfigurationCombo.addItem(item)
    self.selectedConfigurationCombo.setEnabled(False)
    self.selectedConfigurationCombo.setNotify(True)
    self.eventManager.addWidgetEvent(self.selectedConfigurationCombo, self.onSelectedConfigurationComboChanged)


    ###
    # ZoneNotebook and other (combo box to configure selected thing)
    self.zoneConfigurationView = {
            'services'        : {'title' : _("Services")},
            'ports'           : {'title' : _("Ports")},
            'protocols'       : {'title' : _("Protocols")},
            'source_ports'    : {'title' : _("Source Ports")},
            'masquerading'    : {'title' : _("Masquerading")},
            'port_forwarding' : {'title' : _("Port Forwarding")},
            'icmp_filter'     : {'title' : _("ICMP Filter")},
            'rich_rules'      : {'title' : _("Rich Rules")},
            'interfaces'      : {'title' : _("Interfaces")},
            'sources'         : {'title' : _("Sources")},
    }
    # ServiceNotebook
    self.serviceConfigurationView = {
      'ports'         : {'title' : _("Ports")},
      'protocols'     : {'title' : _("Protocols")},
      'source_ports'  : {'title' : _("Source Ports")},
      'modules'       : {'title' : _("Modules")},
      'destinations'  : {'title' : _("Destinations")},
    }
    # ServiceNotebook
    self.ipsecConfigurationView = {
      'entries'       : {'title' : _("Entries")},
    }
    self.configureCombobox = self.factory.createComboBox(hbox,_("Configure"))
    # adding a dummy item to enlarge combobox
    itemColl = self._zoneConfigurationViewCollection()
    self.configureCombobox.addItems(itemColl)
    self.configureCombobox.setNotify(True)
    self.eventManager.addWidgetEvent(self.configureCombobox, self.onSelectedConfigurationChanged)
    ###

    #### Replace Point to change configuration view
    #self.rightPaneFrame = self.factory.createFrame(col2, "TEST")
    self.replacePoint = self.factory.createReplacePoint ( col2 ) #self.rightPaneFrame)
    self.configurationPanel = self.factory.createVBox(self.replacePoint)

    self._replacePointServices()


    #### bottom status lines
    align = self.factory.createLeft(layout)
    statusLine = self.factory.createHBox(align)
    self.statusLabel = self.factory.createLabel(statusLine, self.failed_to_connect_label)
    align = self.factory.createLeft(layout)
    statusLine = self.factory.createHBox(align)
    self.defaultZoneLabel  = self.factory.createLabel(statusLine,     _("Default Zone: {}").format("--------"))
    self.logDeniedLabel = self.factory.createLabel(statusLine,        _("Log Denied: {}").format("--------"))
    self.panicLabel = self.factory.createLabel(statusLine,            _("Panic Mode: {}").format("--------"))
    self.automaticHelpersLabel = self.factory.createLabel(statusLine, _("Automatic Helpers: {}").format("--------"))
    self.lockdownLabel = self.factory.createLabel(statusLine,         _("Lockdown: {}").format("--------"))

    #### buttons on the last line
    align = self.factory.createRight(layout)
    bottomLine = self.factory.createHBox(align)
    aboutButton = self.factory.createPushButton(bottomLine, _("&About") )
    self.eventManager.addWidgetEvent(aboutButton, self.onAbout)
    quitButton = self.factory.createPushButton(bottomLine, _("&Quit"))
    self.eventManager.addWidgetEvent(quitButton, self.onQuitEvent, sendObjOnEvent)

    # Let's test a cancel event
    self.eventManager.addCancelEvent(self.onCancelEvent)
    # Let's check external events every 100 msec
    self.timeout = 100
    #self.eventManager.addTimeOutEvent(self.onTimeOutEvent)
    # End Dialof layout

    self.initFWClient()

  def _replacePointServices(self):
    '''
    draw services frame
    '''
    if len(self.replacePointWidgetsAndCallbacks) > 0:
      print ("Error there are still widget events for ReplacePoint") #TODO log
      return

    if self.configurationPanel.hasChildren():
      print ("Error there are still widgets into ReplacePoint") #TODO log
      return

    #self.mgaFactory.create

    services_header = yui.YTableHeader()
    columns = [ _('Service') ]

    services_header.addColumn("")
    for col in (columns):
        services_header.addColumn(col)

    self.serviceList = self.mgaFactory.createCBTable(self.configurationPanel, services_header, yui.YCBTableCheckBoxOnFirstColumn)
    self.serviceList.setImmediateMode(True)

    if isinstance(self.serviceList, yui.YMGA_CBTable):
      print("YMGA_CBTable")
    self.replacePointWidgetsAndCallbacks.append({'widget': self.serviceList, 'action': self.onRPServiceChecked})
    self.eventManager.addWidgetEvent(self.serviceList, self.onRPServiceChecked)

  def _fillRPServices(self):
    services = None
    if self.runtime_view:
      services = self.fw.listServices()
    else:
      services = self.fw.config().getServiceNames()

    v = []
    for service in services:
      item = yui.YCBTableItem(service)
      item.check(False)
      item.this.own(False)
      v.append(item)

    #NOTE workaround to get YItemCollection working in python
    itemCollection = yui.YItemCollection(v)
    self.serviceList.startMultipleChanges()
    # cleanup old changed items since we are removing all of them
    self.serviceList.setChangedItem(None)
    self.serviceList.addItems(itemCollection)
    self.serviceList.doneMultipleChanges()

  def onRPServiceChecked(self, widgetEvent):
    '''
    works on enabling/disabling service for zone
    '''
    if (widgetEvent.reason() == yui.YEvent.ValueChanged) :
      item = self.serviceList.changedItem()
      if item:
        if item.checked():
          print ("%s checked"%item.cell(0).label())
        else:
          print ("%s unchecked"%item.cell(0).label())

  def _zoneConfigurationViewCollection(self):
    '''
    returns an YItemCollection containing Zone configuration views
    '''
    ordered_configureViews = [ 'services',
                               'ports',
                               'protocols',
                               'source_ports',
                               'masquerading',
                               'port_forwarding',
                               'icmp_filter',
                               'rich_rules',
                               'interfaces',
                               'sources'
    ]
    itemColl = yui.YItemCollection()
    for v in ordered_configureViews:
      item = yui.YItem(self.zoneConfigurationView[v]['title'], False)
      show_item = 'services'
      if show_item == v :
          item.setSelected(True)
      # adding item to views to find the item selected
      self.zoneConfigurationView[v]['item'] = item
      itemColl.push_back(item)
      item.this.own(False)

    return itemColl

  def _serviceConfigurationViewCollection(self):
    '''
    returns an YItemCollection containing Service configuration views
    '''
    ordered_Views = [
      'ports',
      'protocols',
      'source_ports',
      'modules',
      'destinations'
    ]
    itemColl = yui.YItemCollection()
    for v in ordered_Views:
      item = yui.YItem(self.serviceConfigurationView[v]['title'], False)
      show_item = 'ports'
      if show_item == v :
          item.setSelected(True)
      # adding item to views to find the item selected
      self.serviceConfigurationView[v]['item'] = item
      itemColl.push_back(item)
      item.this.own(False)
    return itemColl

  def _ipsecConfigurationViewCollection(self):
    '''
    returns an YItemCollection containing IPSEC configuration views
    '''
    ordered_Views = [
      'entries',
    ]
    itemColl = yui.YItemCollection()
    for v in ordered_Views:
      item = yui.YItem(self.ipsecConfigurationView[v]['title'], False)
      show_item = 'entries'
      if show_item == v :
          item.setSelected(True)
      # adding item to views to find the item selected
      self.ipsecConfigurationView[v]['item'] = item
      itemColl.push_back(item)
      item.this.own(False)

    return itemColl

  def _exception_handler(self, exception_message):
    if not self.__use_exception_handler:
      raise

  def initFWClient(self):
    '''
    initialize firewall client
    '''
    self.fw = client.FirewallClient(wait=1)
    self.__use_exception_handler = True
    self.fw.setExceptionHandler(self._exception_handler)
    self.fw.setNotAuthorizedLoop(True)

    self.fw.connect("connection-changed", self.fwConnectionChanged)
    self.fw.connect("lockdown-enabled", self.lockdown_enabled_cb)
    self.fw.connect("lockdown-disabled", self.lockdown_disabled_cb)
    self.fw.connect("panic-mode-enabled", self.panic_mode_enabled_cb)
    self.fw.connect("panic-mode-disabled", self.panic_mode_disabled_cb)
    self.fw.connect("default-zone-changed", self.default_zone_changed_cb)

    self.fw.connect("config:zone-added", self.conf_zone_changed_cb)
    self.fw.connect("config:zone-updated", self.conf_zone_changed_cb)
    self.fw.connect("config:zone-removed", self.conf_zone_changed_cb)
    self.fw.connect("config:zone-renamed", self.conf_zone_changed_cb)
    self.fw.connect("config:service-added", self.conf_service_changed_cb)
    self.fw.connect("config:service-updated", self.conf_service_changed_cb)
    self.fw.connect("config:service-removed", self.conf_service_changed_cb)
    self.fw.connect("config:service-renamed", self.conf_service_changed_cb)

  def load_zones(self, selected = None):
    '''
    load zones into selectedConfigurationCombo
    '''
    self.selectedConfigurationCombo.startMultipleChanges()
    self.selectedConfigurationCombo.deleteAllItems()

    self.selectedConfigurationCombo.setEnabled(True)
    self.selectedConfigurationCombo.setLabel(self.configureViews['zones']['title'])

    zones = []
    if self.runtime_view:
      zones = self.fw.getZones()
    else:
      zones = self.fw.config().getZoneNames()

    selected_zone = selected
    if selected not in zones:
      selected_zone = self.fw.getDefaultZone()

    # zones
    itemColl = yui.YItemCollection()
    for zone in zones:
      item = yui.YItem(zone, False)
      if zone == selected_zone:
        item.setSelected(True)
      itemColl.push_back(item)
      item.this.own(False)

    self.selectedConfigurationCombo.addItems(itemColl)
    self.selectedConfigurationCombo.doneMultipleChanges()


  def load_services(self, service_name = None):
    '''
    load services into selectedConfigurationCombo
    '''
    self.selectedConfigurationCombo.startMultipleChanges()
    self.selectedConfigurationCombo.deleteAllItems()

    self.selectedConfigurationCombo.setEnabled(True)
    self.selectedConfigurationCombo.setLabel(self.configureViews['services']['title'])

    services = []
    if self.runtime_view:
      services = self.fw.listServices()
    else:
      services = self.fw.config().getServiceNames()

    # services
    itemColl = yui.YItemCollection()
    for service in services:
      item = yui.YItem(service, False)
      if service == service_name:
        item.setSelected(True)
      itemColl.push_back(item)
      item.this.own(False)

    self.selectedConfigurationCombo.addItems(itemColl)
    self.selectedConfigurationCombo.doneMultipleChanges()


  def load_ipsets(self):
    '''
    load ipsets into selectedConfigurationCombo
    '''
    self.selectedConfigurationCombo.startMultipleChanges()
    self.selectedConfigurationCombo.deleteAllItems()

    self.selectedConfigurationCombo.setEnabled(True)
    self.selectedConfigurationCombo.setLabel(self.configureViews['ipsets']['title'])

    ipsets = []
    if self.runtime_view:
        ipsets = self.fw.getIPSets()
    else:
        ipsets = self.fw.config().getIPSetNames()

    # ipsets
    itemColl = yui.YItemCollection()
    for ipset in ipsets:
      item = yui.YItem(ipset, False)
      itemColl.push_back(item)
      item.this.own(False)

    self.selectedConfigurationCombo.addItems(itemColl)
    self.selectedConfigurationCombo.doneMultipleChanges()


#### Firewall events

  def fwConnectionChanged(self):
    '''
    connection changed
    '''
    if self.fw.connected:
      self.fwEventQueue.put({'event': "connection-changed", 'value': True})
      print("connected")
    else:
      self.fwEventQueue.put({'event': "connection-changed", 'value': False})
      print("disc")

  def lockdown_enabled_cb(self):
    '''
    manage lockdown enabled evend from firewalld
    '''
    self.fwEventQueue.put({'event': "lockdown-changed", 'value': True})

  def lockdown_disabled_cb(self):
    '''
    manage lockdown disabled evend from firewalld
    '''
    self.fwEventQueue.put({'event': "lockdown-changed", 'value': False})

  def panic_mode_enabled_cb(self):
    '''
    manage panicmode enabled evend from firewalld
    '''
    self.fwEventQueue.put({'event': "panicmode-changed", 'value': True})

  def panic_mode_disabled_cb(self):
    '''
    manage panicmode disabled evend from firewalld
    '''
    self.fwEventQueue.put({'event': "panicmode-changed", 'value': False})

  def default_zone_changed_cb(self, zone):
    '''
    manage default zone changed from firewalld
    '''
    self.fwEventQueue.put({'event': "default-zone-changed", 'value': zone})

  def conf_zone_changed_cb(self, zone):
    '''
    zones have been modified
    '''
    self.fwEventQueue.put({'event': "config-zone-changed", 'value': zone})

  def conf_service_changed_cb(self, service):
    '''
    service have been modified
    '''
    self.fwEventQueue.put({'event': "config-service-changed", 'value': service})


#### GUI events

  def onCancelEvent(self) :
    '''
    Exit by using cancel event
    '''
    print ("Got a cancel event")

  def onQuitEvent(self, obj) :
    '''
    Exit by using quit button or menu
    '''
    if isinstance(obj, yui.YItem):
      print ("Quit menu pressed")
    else:
      print ("Quit button pressed")
    # BaseDialog needs to force to exit the handle event loop
    self.ExitLoop()


  def onChangeBinding(self, obj):
    '''
    manages changeBindingsButton button pressed
    '''
    print ("TODO: Change binding pressed")

  def onAbout(self) :
    '''
    About dialog invoked
    '''
    ok = common.AboutDialog({
          'name' : self._application_name,
          'dialog_mode' : common.AboutDialogMode.TABBED,
          'version' : VERSION,
          'credits' : _("Credits {}").format("2019 Angelo Naselli"),
          'license' : 'GPLv2+',
          'authors' : 'Angelo Naselli &lt;anaselli@linux.it&gt;',
          'description' : _("{}  is a graphical configuration tool for firewalld.").format(PROJECT),
          'size': {'column': 50, 'lines': 6},
    })

  def onChangeView(self):
    '''
    manages currentViewCombobox chenges
    '''
    item = self.currentViewCombobox.selectedItem()
    self.runtime_view = item == self.views['runtime']['item']
    # Let's change view as if a new configuration has been chosen
    self.onConfigurationViewChanged()
    self.editFrameBox.setEnabled(not self.runtime_view)


  def onEditFrameAddButtonEvent(self):
    '''
    from edit frame Add has benn pressed, let's call the right add event
    '''
    item = self.configureViewCombobox.selectedItem()
    if item == self.configureViews['zones']['item']:
      #Zones selected
      self.onAddZone()
    elif item == self.configureViews['services']['item']:
      #Services selected
      self.onServiceConfAddService()
    elif item == self.configureViews['ipsets']['item']:
      # ip sets selected
      pass

  def onEditFrameEditButtonEvent(self):
    '''
    from edit frame Edit has benn pressed, let's call the right edit event
    '''
    item = self.configureViewCombobox.selectedItem()
    if item == self.configureViews['zones']['item']:
      #Zones selected
      self.onEditZone()
    elif item == self.configureViews['services']['item']:
      #Services selected
      self.onServiceConfEditService()
    elif item == self.configureViews['ipsets']['item']:
      # ip sets selected
      pass

  def onEditFrameRemoveButtonEvent(self):
    '''
    from edit frame Remove has benn pressed, let's call the right remove event
    '''
    item = self.configureViewCombobox.selectedItem()
    if item == self.configureViews['zones']['item']:
      #Zones selected
      self.onRemoveZone()
    elif item == self.configureViews['services']['item']:
      #Services selected
      self.onServiceConfRemoveService()
    elif item == self.configureViews['ipsets']['item']:
      # ip sets selected
      pass

  def onEditFrameLoadDefaultsButtonEvent(self):
    '''
    from edit frame Remove has benn pressed, let's call the right remove event
    '''
    item = self.configureViewCombobox.selectedItem()
    if item == self.configureViews['zones']['item']:
      #Zones selected
      self.onLoadDefaultsZone()
    elif item == self.configureViews['services']['item']:
      #Services selected
      self.onServiceConfLoadDefaultsService()
    elif item == self.configureViews['ipsets']['item']:
      # ip sets selected
      pass

  def onAddZone(self):
    '''
    manages add zone button
    '''
    if self.runtime_view:
      return
    self._add_edit_zone(True)
    self.load_zones()

  def onRemoveZone(self):
    '''
    manages remove zone button
    '''
    if self.runtime_view:
      return
    selected_zoneitem = self.selectedConfigurationCombo.selectedItem()
    if selected_zoneitem:
      selected_zone = selected_zoneitem.label()
      zone = self.fw.config().getZoneByName(selected_zone)
      zone.remove()
      self.load_zones()
      # TODO self.onChangeZone()

  def onEditZone(self):
    '''
    manages edit zone button
    '''
    if self.runtime_view:
      return
    self._add_edit_zone(False)
    selected_zone = None
    selected_zoneitem = self.selectedConfigurationCombo.selectedItem()
    if selected_zoneitem:
      selected_zone = selected_zoneitem.label()
    self.load_zones(selected_zone)

  def onLoadDefaultsZone(self):
    '''
    manages load defaults zone Button
    '''
    if self.runtime_view:
      return
    selected_zoneitem = self.selectedConfigurationCombo.selectedItem()
    if selected_zoneitem:
      selected_zone = selected_zoneitem.label()
      zone = self.fw.config().getZoneByName(selected_zone)
      zone.loadDefaults()
      # TODO self.onChangeZone()

  def _add_edit_zone(self, add):
    '''
    adds or edit zone (parameter add True if adding)
    '''
    zoneBaseInfo = {}
    zoneBaseInfo['max_zone_name_len'] = functions.max_zone_name_len()
    if not add:
      # fill zoneBaseInfo for zoneBaseDialog fields
      selected_zoneitem = self.selectedConfigurationCombo.selectedItem()
      if selected_zoneitem:
        selected_zone = selected_zoneitem.label()
        zone = self.fw.config().getZoneByName(selected_zone)
        settings = zone.getSettings()
        props = zone.get_properties()
        zoneBaseInfo['name'] = zone.get_property("name")
        zoneBaseInfo['version'] = settings.getVersion()
        zoneBaseInfo['short'] = settings.getShort()
        zoneBaseInfo['description'] = settings.getDescription()
        zoneBaseInfo['default'] = props["default"]
        zoneBaseInfo['builtin'] = props["builtin"]
        zoneBaseInfo['target'] = settings.getTarget()
        if zoneBaseInfo['target'] == DEFAULT_ZONE_TARGET:
          zoneBaseInfo['target'] = 'default'

    zoneBaseDlg = zoneBaseDialog.ZoneBaseDialog(zoneBaseInfo)
    newZoneBaseInfo = zoneBaseDlg.run()
    # Cancelled if None is returned
    if newZoneBaseInfo is None:
      return

    if not add:
      if zoneBaseInfo['name']        == newZoneBaseInfo['name'] and \
         zoneBaseInfo['version']     == newZoneBaseInfo['version'] and  \
         zoneBaseInfo['short']       == newZoneBaseInfo['short'] and \
         zoneBaseInfo['description'] == newZoneBaseInfo['description'] and \
         zoneBaseInfo['target']      == newZoneBaseInfo['target']:
        # no changes
        return
      selected_zoneitem = self.selectedConfigurationCombo.selectedItem()
      if selected_zoneitem:
        selected_zone = selected_zoneitem.label()
        zone = self.fw.config().getZoneByName(selected_zone)
        if zoneBaseInfo['version'] != newZoneBaseInfo['version'] or  \
         zoneBaseInfo['short'] != newZoneBaseInfo['short'] or \
         zoneBaseInfo['description'] != newZoneBaseInfo['description'] or \
         zoneBaseInfo['target'] != newZoneBaseInfo['target']:
          settings = zone.getSettings()
          settings.setVersion(newZoneBaseInfo['version'])
          settings.setShort(newZoneBaseInfo['short'])
          settings.setDescription(newZoneBaseInfo['description'])
          settings.setTarget(newZoneBaseInfo['target'])
          zone.update(settings)
        if zoneBaseInfo['name'] == newZoneBaseInfo['name']:
          return
        zone.rename(newZoneBaseInfo['name'])
    else:
      settings = client.FirewallClientZoneSettings()
      settings.setVersion(newZoneBaseInfo['version'])
      settings.setShort(newZoneBaseInfo['short'])
      settings.setDescription(newZoneBaseInfo['description'])
      settings.setTarget(newZoneBaseInfo['target'])
      self.fw.config().addZone(newZoneBaseInfo['name'], settings)

  def onServiceConfAddService(self, *args):
    '''
    manages add service button
    '''
    if self.runtime_view:
      return
    self._add_edit_service(True)

  def onServiceConfRemoveService(self, *args):
    '''
    manages remove zone button
    '''
    if self.runtime_view:
      return
    selected_item = self.selectedConfigurationCombo.selectedItem()
    if selected_item:
      active_service = selected_item.label()
      service = self.fw.config().getServiceByName(active_service)
      service.remove()
      self.load_services()
      # TODO self.onChangeService()

  def onServiceConfEditService(self, *args):
    if self.runtime_view:
      return
    self._add_edit_service(False)

  def onServiceConfLoadDefaultsService(self, *args):
    '''
    manages load defaults service Button
    '''
    if self.runtime_view:
      return
    selected_item = self.selectedConfigurationCombo.selectedItem()
    if selected_item:
      active_service = selected_item.label()
      service = self.fw.config().getServiceByName(active_service)
      service.loadDefaults()
      # TODO self.onChangeService()

  def _add_edit_service(self, add):
    '''
    adds or edit service (parameter add True if adding)
    '''
    serviceBaseInfo = {}
    if not add:
      # fill serviceBaseInfo for serviceBaseDialog fields
      selected_serviceitem = self.selectedConfigurationCombo.selectedItem()
      if selected_serviceitem:
        active_service = selected_serviceitem.label()
        service = self.fw.config().getServiceByName(active_service)
        settings = service.getSettings()
        props = service.get_properties()
        serviceBaseInfo['default']     = props["default"]
        serviceBaseInfo['builtin']     = props["builtin"]
        serviceBaseInfo['name']        = service.get_property("name")
        serviceBaseInfo['version']     = settings.getVersion()
        serviceBaseInfo['short']       = settings.getShort()
        serviceBaseInfo['description'] = settings.getDescription()

    serviceBaseDlg = serviceBaseDialog.ServiceBaseDialog(serviceBaseInfo)
    newServiceBaseInfo = serviceBaseDlg.run()
    # Cancelled if None is returned
    if newServiceBaseInfo is None:
      return

    if not add:
      if serviceBaseInfo['name']        == newServiceBaseInfo['name'] and \
         serviceBaseInfo['version']     == newServiceBaseInfo['version'] and  \
         serviceBaseInfo['short']       == newServiceBaseInfo['short'] and \
         serviceBaseInfo['description'] == newServiceBaseInfo['description']:
        # no changes
        return

      selected_serviceitem = self.selectedConfigurationCombo.selectedItem()
      if selected_serviceitem:
        selected_service = selected_serviceitem.label()
        service = self.fw.config().getServiceByName(active_service)

        if serviceBaseInfo['version'] != newServiceBaseInfo['version'] or  \
           serviceBaseInfo['short'] != newServiceBaseInfo['short'] or \
           serviceBaseInfo['description'] != newServiceBaseInfo['description']:
          settings = service.getSettings()
          settings.setVersion(newServiceBaseInfo['version'])
          settings.setShort(newServiceBaseInfo['short'])
          settings.setDescription(newServiceBaseInfo['description'])
          service.update(settings)
        if serviceBaseInfo['name'] == newServiceBaseInfo['name']:
          return
        service.rename(newServiceBaseInfo['name'])
    else:
      settings = client.FirewallClientServiceSettings()
      settings.setVersion(newServiceBaseInfo['version'])
      settings.setShort(newServiceBaseInfo['short'])
      settings.setDescription(newServiceBaseInfo['description'])
      self.fw.config().addService(newServiceBaseInfo['name'], settings)



  def onSelectedConfigurationComboChanged(self):
    '''
    depending on what configuration view is selected it manages zones,
    services, etc
    '''
    item = self.configureViewCombobox.selectedItem()
    if item == self.configureViews['zones']['item']:
      #Zones selected
      # self.onChangeZone()
      pass
    elif item == self.configureViews['services']['item']:
      #Services selected
      pass
    elif item == self.configureViews['ipsets']['item']:
      # ip sets selected
      pass

  def onConfigurationViewChanged(self):
    '''
    manages configureViewCombobox changes
    '''
    item = self.configureViewCombobox.selectedItem()
    if item == self.configureViews['zones']['item']:
      #Zones selected
      self.load_zones()
      self.configureCombobox.startMultipleChanges()
      self.configureCombobox.deleteAllItems()
      itemColl = self._zoneConfigurationViewCollection()
      self.configureCombobox.addItems(itemColl)
      self.configureCombobox.setEnabled(True)
      self.configureCombobox.doneMultipleChanges()
      self.editFrameBox.setLabel(_("Edit zones"))
    elif item == self.configureViews['services']['item']:
      #Services selected
      self.load_services()
      self.configureCombobox.startMultipleChanges()
      self.configureCombobox.deleteAllItems()
      itemColl = self._serviceConfigurationViewCollection()
      self.configureCombobox.addItems(itemColl)
      self.configureCombobox.setEnabled(True)
      self.configureCombobox.doneMultipleChanges()
      self.editFrameBox.setLabel(_("Edit services"))
    elif item == self.configureViews['ipsets']['item']:
      # ip sets selected
      self.load_ipsets()
      self.configureCombobox.startMultipleChanges()
      self.configureCombobox.deleteAllItems()
      itemColl = self._ipsecConfigurationViewCollection()
      self.configureCombobox.addItems(itemColl)
      self.configureCombobox.setEnabled(True)
      self.configureCombobox.doneMultipleChanges()
      self.editFrameBox.setLabel(_("Edit ipsets"))
    else:
      # disabling info combo
      self.selectedConfigurationCombo.startMultipleChanges()
      self.selectedConfigurationCombo.deleteAllItems()
      self.selectedConfigurationCombo.setLabel("     ")
      self.selectedConfigurationCombo.setEnabled(False)
      self.selectedConfigurationCombo.doneMultipleChanges()
      #disabling configure view
      self.configureCombobox.startMultipleChanges()
      self.configureCombobox.deleteAllItems()
      self.configureCombobox.setEnabled(False)
      self.configureCombobox.doneMultipleChanges()
      self.editFrameBox.setLabel("")


  def onSelectedConfigurationChanged(self):
    '''
    manages configureCombobox changes
    '''
    config_item = self.configureCombobox.selectedItem()

    item = self.configureViewCombobox.selectedItem()
    if item == self.configureViews['zones']['item']:
      #Zones selected
      if config_item == self.zoneConfigurationView['services']['item']:
        self._fillRPServices()
    elif item == self.configureViews['services']['item']:
      #Services selected
      pass
    elif item == self.configureViews['ipsets']['item']:
      # ip sets selected
      pass

  def onTimeOutEvent(self):
    print ("Timeout occurred")

  def doSomethingIntoLoop(self):
    '''
    check on internal queue if any fw event has been managed
    '''
    try:
      item = self.fwEventQueue.get_nowait()
      # managing deferred firewall events
      if item['event'] == 'connection-changed':
        connected = item['value']
        self.connection_lost = not connected
        t = self.connected_label if connected else self.trying_to_connect_label
        self.statusLabel.setText(t)
        if connected:
          self.fw.authorizeAll()
          default_zone = self.fw.getDefaultZone()
          self.defaultZoneLabel.setText(_("Default Zone: {}").format(default_zone))
          self.log_denied = self.fw.getLogDenied()
          self.logDeniedLabel.setText(("Log Denied: {}").format(self.log_denied))
          self.automatic_helpers = self.fw.getAutomaticHelpers()
          self.automaticHelpersLabel.setText(_("Automatic Helpers: {}").format(self.automatic_helpers))
          #### TODO self.set_automaticHelpersLabel(self.automatic_helpers)
          lockdown = self.fw.queryLockdown()
          t = self.enabled if lockdown else self.disabled
          self.lockdownLabel.setText(_("Lockdown: {}").format(t))
          panic = self.fw.queryPanicMode()
          t = self.enabled if panic else self.disabled
          self.panicLabel.setText(_("Panic Mode: {}").format(t))
        else:
          self.defaultZoneLabel.setText(_("Default Zone: {}").format("--------"))
          self.logDeniedLabel.setText(("Log Denied: {}").format("--------"))
          self.automaticHelpersLabel.setText(_("Automatic Helpers: {}").format("--------"))
          self.lockdownLabel.setText(_("Lockdown: {}").format("--------"))
          self.panicLabel.setText(_("Panic Mode: {}").format("--------"))
        item = self.configureViewCombobox.selectedItem()
        if item == self.configureViews['zones']['item']:
          self.load_zones()


      elif item['event'] == 'lockdown-changed':
        t = self.enabled if item['value'] else self.disabled
        self.lockdownLabel.setText(_("Lockdown: {}").format(t))
        # TODO manage menu items if needed
      elif item['event'] == 'panicmode-changed':
        t = self.enabled if item['value'] else self.disabled
        self.panicLabel.setText(_("Panic Mode: {}").format(t))
        # TODO manage menu items if needed
      elif item['event'] == 'default-zone-changed':
        zone = item['value']
        self.defaultZoneLabel.setText(_("Default Zone: {}").format(zone))
        # TODO self.update_active_zones()
      elif item['event'] == 'config-zone-changed':
        zone = item['value']
        if not self.runtime_view:
          item = self.configureViewCombobox.selectedItem()
          if item == self.configureViews['zones']['item']:
            # Zones selected
            selected_zone = None
            selected_item = self.selectedConfigurationCombo.selectedItem()
            if selected_item:
              selected_zone = selected_item.label()
            self.load_zones(selected_zone)
      elif item['event'] == 'config-service-changed':
        service = item['value']
        if not self.runtime_view:
          item = self.configureViewCombobox.selectedItem()
          if item == self.configureViews['services']['item']:
            # Services selected
            selected_service = None
            selected_item = self.selectedConfigurationCombo.selectedItem()
            if selected_item:
              selected_service = selected_item.label()
            self.load_services(selected_service)
    except Empty as e:
      pass

