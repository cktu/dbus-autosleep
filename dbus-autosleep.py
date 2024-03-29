#!/usr/bin/env python
 
from dbus.mainloop.glib import DBusGMainLoop

try:
  import gobject  # Python 2.x
except:
  from gi.repository import GLib as gobject # Python 3.x

import dbus
import dbus.service
import platform
import argparse
import logging
import sys
import traceback
import os
import os.path
import ctypes
import time

log = logging.getLogger("DbusAutosleep")

sys.path.insert(1, os.path.join(os.path.dirname(__file__), '/opt/victronenergy/dbus-modem'))
from vedbus import VeDbusService

# ----------------------------------------------------------------
# --- PARAMETERS -------------------------------------------------
# ----------------------------------------------------------------

# dbus path for Victron energy storage system
ESS_PATH = 'com.victronenergy.vebus.ttyS3'

# dbus path for grid meter
GRID_METER_PATH = 'com.victronenergy.grid.grid_id00'

# dbus path for PV inverter
PV_INVERTER_PATH = 'com.victronenergy.pvinverter.pv0.pvinverter_id00'

# Debounce time for charge/feed-in timeouts [s]
THRESHOLD_DEBOUNCE = 10

# Feed-in timer to disable inverter [s]
FEED_IN_TIMEOUT = 3600

# Grid threshold for inverter activation [W]
FEED_IN_THRESHOLD = 100

# Grid threshold for inverter deactivation [W]
FEED_IN_DISABLE_THRESHOLD = 50

# Charge timer to disable charger [s]
CHARGE_TIMEOUT = 3600

# Grid threshold for charger activation [W]
CHARGE_THRESHOLD = -100

# Grid threshold for charger deactivation [W]
CHARGE_DISABLE_THRESHOLD = -50

# Stabilisation time for feed in or charge request [s]
STABLE_TIMER = 60

# Minimum time between changes of inverter mode [s]
LOCK_TIME = 600

# ----------------------------------------------------------------
# ----------------------------------------------------------------
# ----------------------------------------------------------------

VERSION     = "0.2"

# Have a mainloop, so we can send/receive asynchronous calls to and from dbus
DBusGMainLoop(set_as_default=True)

root = logging.getLogger()
root.setLevel(logging.INFO)

handler = logging.StreamHandler(sys.stdout)
handler.setLevel(logging.INFO)
formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
handler.setFormatter(formatter)
root.addHandler(handler)

log.info('Startup')

# Probably not all of these needed this is just duplicating the Victron code.
class SystemBus(dbus.bus.BusConnection):
    def __new__(cls):
        return dbus.bus.BusConnection.__new__(cls, dbus.bus.BusConnection.TYPE_SYSTEM)
 
class SessionBus(dbus.bus.BusConnection):
    def __new__(cls):
        return dbus.bus.BusConnection.__new__(cls, dbus.bus.BusConnection.TYPE_SESSION)
 
def dbusconnection():
    return SessionBus() if 'DBUS_SESSION_BUS_ADDRESS' in os.environ else SystemBus()

VE_MODE_CHARGER_ONLY = 1
VE_MODE_INVERTER_ONLY = 2
VE_MODE_ON = 3
VE_MODE_OFF = 4
def get_ve_mode_text(state):
    if state == VE_MODE_CHARGER_ONLY:
        return 'Charger only'
    elif state == VE_MODE_INVERTER_ONLY:
        return 'Inverter only'
    elif state == VE_MODE_ON:
        return 'On'
    elif state == VE_MODE_OFF:
        return 'Off'
    else:
        return 'UNKNOWN'

grid_import_debounce = THRESHOLD_DEBOUNCE
grid_import_timer = 0
grid_export_debounce = THRESHOLD_DEBOUNCE
grid_export_timer = 0
feed_in_unstable = False
feed_in_stable = False
feed_in_stable_timer = STABLE_TIMER
charge_unstable = False
charge_stable = False
charge_stable_timer = STABLE_TIMER
mode_current = 0  # Updated with actual value during script startup
mode_change_lock_timer = LOCK_TIME
output_enabled = True  # Initialize to true, otherwise we would reset the mode_change_lock_timer and thus output the mode immediately after startup when output is enabled

def update_ess_mode():
    try:
        feed_in_allowed = (busitem['DisableFeedIn'].GetValue() == 0)
        charge_allowed = (busitem['DisableCharge'].GetValue() == 0)
        dbusservice['debug']['/FeedInAllowed'] = feed_in_allowed
        dbusservice['debug']['/ChargeAllowed'] = charge_allowed
    except:
        # In case getting these values from the bus fails, assume everything is okay
        # This can happen when the multi has not been active since the GX rebooted
        feed_in_allowed = True
        charge_allowed = True

    try:
        # Get power of the ESS system
        # Positive means power drawn, negative power fed in
        ess_power = busitem['EssPower'].GetValue()
        dbusservice['debug']['/EssPower'] = ess_power
        if not isinstance(ess_power, dbus.Int32): ess_power = 0
    except:
        # Assume 0 if value is not available
        ess_power = 0

    try:
        # Get power exported to or imported from grid
        # Positive means import, negative means export
        grid_power = busitem['GridPower'].GetValue()
        dbusservice['debug']['/GridPower'] = grid_power
    except:
        # Grid power not available. I cannot work like that!
        # Wait 30 seconds and exit (to be restarted by supervisor)
        log.error('Could not get grid power, terminating in 30 seconds')
        time.sleep(30)
        sys.exit()

    try:
        # Get PV inverter mode and power generated by PV
        pv_inverter_mode = busitem['PvStatus'].GetValue()
        pv_power = busitem['PvPower'].GetValue()
        dbusservice['debug']['/PvPower'] = pv_power
        dbusservice['debug']['/PvInverterStatus'] = pv_inverter_mode
    except:
        pv_inverter_mode = 0
        pv_power = -1

    # No we've got all data, do the actual work
    try:
        # Calculate residual load, i.e. load not supplied by PV
        # = Load - PV generation
        # = Grid power - ESS power
        residual_load = grid_power - ess_power
        dbusservice['debug']['/ResidualLoad'] = residual_load

        # Calculate total load (diagnosis only)
        total_load = residual_load + pv_power
        dbusservice['debug']['/TotalLoad'] = total_load


        ###############################################
        ### Determine and stabilize feed in request ###
        ###############################################

        # Debounce feed-in trigger
        # grid_import_debounce == 0 means the trigger has been stable for THRESHOLD_DEBOUNCE
        global grid_import_debounce
        feed_in_threshold_exceeded = residual_load > FEED_IN_DISABLE_THRESHOLD
        if not feed_in_threshold_exceeded:
            grid_import_debounce = THRESHOLD_DEBOUNCE
        elif grid_import_debounce > 0:
            grid_import_debounce = grid_import_debounce - 1
        dbusservice['debug']['/FeedInDebounce'] = grid_import_debounce

        # Handle feed-in timeout
        # This determines whether more power than supplied by PV was used within FEED_IN_TIMEOUT
        # When grid_import_timer == 0, PV was able to supply the load for at least FEED_IN_TIMEOUT
        global grid_import_timer
        if grid_import_debounce == 0:
            grid_import_timer = FEED_IN_TIMEOUT
        elif grid_import_timer > 0:
            grid_import_timer = grid_import_timer - 1
        dbusservice['debug']['/FeedInTimeout'] = grid_import_timer

        # Determine whether the inverter should be active
        # This is the case if 
        #   (a) the Victron control allows feed-in
        #       (this includes the supervision of the battery's state of charge), and
        #   (b1a) feed-in request is currently set (for some hysteresis), and
        #   (b1b) PV has not been able to supply the load within FEED_IN_TIMEOUT
        #         (this avoids the inverter to remain active when there is enough PV power anyway), or
        #   (b2a) feed-in request is currently not set (again for hysteresis), and
        #   (b2b) PV is currently unable to supply the load.
        global feed_in_stable
        feed_in_threshold_exceeded = residual_load > FEED_IN_THRESHOLD
        feed_in_new = feed_in_allowed and ((feed_in_stable and (grid_import_timer > 0)) or ((not feed_in_stable) and feed_in_threshold_exceeded))
        dbusservice['debug']['/FeedInRequest'] = feed_in_new

        # Stabilize feed-in request
        # feed_in_unstable is the candidate to be stabilized
        # feed_in_stable_timer is the time until the request can be considered stable, 0 means current state is stable
        # feed_in_stable contains the latest state to be found stable
        global feed_in_unstable, feed_in_stable_timer
        if (feed_in_new != feed_in_unstable):
            feed_in_unstable = feed_in_new
            feed_in_stable_timer = STABLE_TIMER
        elif (feed_in_stable_timer > 0):
            feed_in_stable_timer = feed_in_stable_timer - 1
        else:
            feed_in_stable = feed_in_unstable

        dbusservice['debug']['/FeedInRequestStableTimer'] = feed_in_stable_timer
        dbusservice['debug']['/FeedInRequestStable'] = feed_in_stable


        ##############################################
        ### Determine and stabilize charge request ###
        ##############################################

        # Check PV inverter state
        pv_active = (pv_inverter_mode == 11) or (pv_inverter_mode == 12)

        # Debounce charge trigger
        # grid_export_debounce == 0 means the trigger has been stable for THRESHOLD_DEBOUNCE
        global grid_export_debounce
        charge_threshold_exceeded = residual_load < CHARGE_DISABLE_THRESHOLD  # exceeded means more negative
        if not charge_threshold_exceeded:
            grid_export_debounce = THRESHOLD_DEBOUNCE
        elif grid_export_debounce > 0:
            grid_export_debounce = grid_export_debounce - 1
        dbusservice['debug']['/ChargeDebounce'] = grid_export_debounce

        # Handle charge timeout
        # This determines whether excess power was available within CHARGE_TIMEOUT
        # When grid_export_timer == 0, no excess power was available for at least CHARGE_TIMEOUT
        global grid_export_timer
        if grid_export_debounce == 0:
            grid_export_timer = CHARGE_TIMEOUT
        elif grid_export_timer > 0:
            grid_export_timer = grid_export_timer - 1
        dbusservice['debug']['/ChargeTimeout'] = grid_export_timer

        # Determine whether the charger should be active
        # This is the case if
        #   (a) the Victron control allows charging
        #       (this includes the supervision of the battery's state of charge), and
        #   (b1a) charge request is currently set (for some hysteresis), and
        #   (b1b) excess power was available within CHARGE_TIMEOUT
        #         (this avoids the charger to remain active when there is not enough PV power anyway), or
        #   (b2a) charge request is currently not set (again for hysteresis), and
        #   (b2b) excess power is currently available, and
        #   (c) PV inverter is active (this speeds up the shutdown when PV inverter goes to sleep).
        global charge_stable
        charge_threshold_exceeded = residual_load < CHARGE_THRESHOLD  # exceeded means more negative
        charge_new = charge_allowed and ((charge_stable and (grid_export_timer > 0)) or ((not charge_stable) and charge_threshold_exceeded)) and pv_active
        dbusservice['debug']['/ChargeRequest'] = charge_new

        # Stabilize charge request
        # charge_unstable is the candidate to be stabilized
        # charge_stable_timer is the time until the request can be considered stable, 0 means current state is stable
        # charge_stable contains the latest state to be found stable
        global charge_unstable, charge_stable_timer
        if (charge_new != charge_unstable):
            charge_unstable = charge_new
            charge_stable_timer = STABLE_TIMER
        elif (charge_stable_timer > 0):
            charge_stable_timer = charge_stable_timer - 1
        else:
            charge_stable = charge_unstable

        dbusservice['debug']['/ChargeRequestStableTimer'] = charge_stable_timer
        dbusservice['debug']['/ChargeRequestStable'] = charge_stable


        ######################################
        ### Determine and set overall mode ###
        ######################################

        # Determine desired mode
        if (feed_in_stable == True):
            # Inverter only does not work, so we're always using 'On' when we want to feed in.
            mode_new = VE_MODE_ON
        elif (charge_stable == True):
            # Charger only seems not to bring much benefit (except more mode changes), so we're not using it.
            #mode_new = VE_MODE_CHARGER_ONLY
            mode_new = VE_MODE_ON
        else:
            mode_new = VE_MODE_OFF

        dbusservice['debug']['/Mode'] = mode_new

        # Determine whether output is enabled
        global output_enabled, mode_current, mode_change_lock_timer
        output_enabled_old = output_enabled
        output_enabled = os.path.isfile('/data/dbus-autosleep/.output-enabled')
        if output_enabled and (not output_enabled_old):
            mode_current = busitem['Mode'].GetValue()
            log.info(f"Automatic mode change enabled, current mode '{get_ve_mode_text(mode_current)}'")
            mode_change_lock_timer = 0
        elif (not output_enabled) and output_enabled_old:
            log.info('Automatic mode change disabled')

        # Check whether we are allowed to update the mode
        # In case the mode was changed less than LOCK_TIME ago, we don't change anything to avoid too frequent toggling
        # mode_change_lock_timer == 0 means we can change the mode when we feel to do so
        if (mode_change_lock_timer > 0):  # Mode change is still locked
            mode_change_lock_timer = mode_change_lock_timer - 1
        else:  # Mode change is unlocked

            # Check whether we want to update the mode
            if (mode_current != mode_new):

                # Write log entry for this remarkable event
                battery_soc = busitem['BatterySoc'].GetValue()
                log.info(f"{'Changing mode' if output_enabled else 'Suggesting mode change'} from '{get_ve_mode_text(mode_current)}' to '{get_ve_mode_text(mode_new)}'")
                log.info(f'  PV state: {pv_inverter_mode}, PV power: {pv_power:.0f} W, ESS power: {ess_power:.0f} W')
                log.info(f'  Grid power: {grid_power:.0f} W, Residual load: {residual_load:.0f} W, Total load: {total_load:.0f} W')
                log.info(f'  Battery SoC: {battery_soc:.0f}%, Charge allowed: {charge_allowed}, Feed-In allowed: {feed_in_allowed}')
                log.info(f'  Charge timeout counter: {grid_export_timer}, Feed-in timeout counter: {grid_import_timer}')

                # Update the mode
                mode_current = mode_new
                mode_change_lock_timer = LOCK_TIME
                dbusservice['debug']['/ModeOutput'] = mode_current
                if output_enabled:
                    busitem['Mode'].SetValue(dbus.UInt32(mode_current, variant_level=1))

        dbusservice['debug']['/ModeLockTime'] = mode_change_lock_timer

    except:
        log.error('Exception in update_ess_mode')
        log.error(traceback.format_exc())
        # Wait 10 seconds and exit (to be restarted by supervisor)
        time.sleep(10)
        sys.exit()

    return True

 
# Here is the bit you need to create multiple new services - try as much as possible timplement the Victron Dbus API requirements.
def new_service(base, type, physical, instance):
    self =  VeDbusService(f"{base}.{type}.{physical}", dbusconnection())

    # Create the management objects, as specified in the ccgx dbus-api document
    self.add_path('/Mgmt/ProcessName', __file__)
    self.add_path('/Mgmt/ProcessVersion', 'Unknown version, and running on Python ' + platform.python_version())
    self.add_path('/Connected', 1)  
    self.add_path('/HardwareVersion', 0)

    # Create device type specific objects
    if physical == 'dbus_autosleep':
        self.add_path('/Mgmt/Connection', 'No connection')
        self.add_path('/CustomName', 'Debug data for dbus-solaredge.py')
        self.add_path('/GridPower', None)
        self.add_path('/EssPower', None)
        self.add_path('/ResidualLoad', None)
        self.add_path('/TotalLoad', None)
        self.add_path('/FeedInDebounce', None)
        self.add_path('/FeedInTimeout', None)
        self.add_path('/FeedInAllowed', None)
        self.add_path('/FeedInRequest', None)
        self.add_path('/FeedInRequestStableTimer', None)
        self.add_path('/FeedInRequestStable', None)
        self.add_path('/PvInverterStatus', None)
        self.add_path('/PvPower', None)
        self.add_path('/ChargeDebounce', None)
        self.add_path('/ChargeTimeout', None)
        self.add_path('/ChargeAllowed', None)
        self.add_path('/ChargeRequest', None)
        self.add_path('/ChargeRequestStableTimer', None)
        self.add_path('/ChargeRequestStable', None)
        self.add_path('/Mode', None)
        self.add_path('/ModeLockTime', None)
        self.add_path('/ModeOutput', None)

    return self

dbusservice = {} # Dictonary to hold the multiple services

# Create dbus service
dbusservice['debug']          = new_service('com.victronenergy', 'debug', 'dbus_autosleep', 22)

# Bus items to be read/written
busitem = {}
try:
    SYSTEM_PATH = 'com.victronenergy.system'
    busitem['DisableFeedIn'] = dbusconnection().get_object(ESS_PATH, '/Hub4/DisableFeedIn')
    busitem['DisableCharge'] = dbusconnection().get_object(ESS_PATH, '/Hub4/DisableCharge')
    busitem['GridPower']     = dbusconnection().get_object(GRID_METER_PATH, '/Ac/Power')
    busitem['PvPower']       = dbusconnection().get_object(PV_INVERTER_PATH, '/Ac/Power')
    busitem['PvStatus']      = dbusconnection().get_object(PV_INVERTER_PATH, '/StatusCode')
    busitem['Mode']          = dbusconnection().get_object(ESS_PATH, '/Mode')
    busitem['BatterySoc']    = dbusconnection().get_object(SYSTEM_PATH, '/Dc/Battery/Soc')
    busitem['EssPower']      = dbusconnection().get_object(ESS_PATH, '/Ac/ActiveIn/L1/P')
except:
    log.error('Required bus items not found, terminating in 10 seconds')
    time.sleep(10)
    sys.exit()


mode_current = busitem['Mode'].GetValue()
log.info(f'Current mode: {get_ve_mode_text(mode_current)}')

# Everything done so just set a time to run an update function to update the data values every second.
gobject.timeout_add(1000, update_ess_mode)

log.info('Connected to dbus, and switching over to GLib.MainLoop()')

mainloop = gobject.MainLoop()
mainloop.run()
