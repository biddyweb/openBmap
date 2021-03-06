#!/usr/bin/python

# Copyright 2008, 2009 Ronan DANIELLOU
# Copyright 2008, 2009 Onen (onen.om@free.fr)
# Copyright 2011 Matija Nalis (mnalis-openmoko@voyager.hr)
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, version 3 of the License.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.

import gobject
import dbus
import sys
import dbus.mainloop.glib
import time
from datetime import datetime
import inspect
import logging
import ConfigParser
import threading
import os.path
import urllib2
import math
import plugins.obmplugin

# HTTP multi part upload
import Upload

class Gsm:
    # This lock will be used when reading/updating *any* GSM related variable.
    # Thus MCC, MNC, lac, cellid and strength are consistent.
    lock = threading.Lock()
    
    def __init__(self, bus):
        # "MCC", "MNC", "lac", "cid" and "strength" are received asynchronuously, through signal handler
        # thus we need to store them for the time the logging loop runs
        self._lac = -1
        self._cid = -1
        self._strength = -1
        self._networkAccessType = ''
        self._MCC = ''
        self._MNC = ''
        self._registrationGSM = ''
        
        #This will remember all the cells seen. The structure is:
        # {id}->{MCC}->{MNC}->( {LAC}->[cid], {LAC}->[cid] ) 0: servings, 1: neighbours
        self._remember_cells_structures = {}
        self._current_remember_cells_structure_id = None
        self.REMEMBER_CELLS_STRUCTURE_TOTAL_ID = 'Total number of cells since Launch'
        self.set_current_remember_cells_id(self.REMEMBER_CELLS_STRUCTURE_TOTAL_ID)
        self._nb_remember_servings_in_last_structure = 0
        self._nb_remember_neighbours_in_last_structure = 0
        self._nb_remember_servings_total = 0
        self._nb_remember_neighbours_total = 0

        self._manufacturer = 'N/A'
        self._model = 'N/A'
        self._revision = 'N/A'
        self.get_device_info()
        self._observers = []
        self._call_ongoing = False
        
        if bus:
            bus.add_signal_receiver(self.network_status_handler,
                                          'Status',
                                          'org.freesmartphone.GSM.Network',
                                          'org.freesmartphone.ogsmd',
                                          '/org/freesmartphone/GSM/Device')
            bus.add_signal_receiver(self.signal_strength_handler,
                                          'SignalStrength',
                                          'org.freesmartphone.GSM.Network',
                                          'org.freesmartphone.ogsmd',
                                          '/org/freesmartphone/GSM/Device')
            bus.add_signal_receiver(self.call_status_handler,
                                          'CallStatus',
                                          'org.freesmartphone.GSM.Call',
                                          'org.freesmartphone.ogsmd',
                                          '/org/freesmartphone/GSM/Device')
            self._gsmMonitoringIface = dbus.Interface( bus.get_object('org.freesmartphone.ogsmd', '/org/freesmartphone/GSM/Device'),
                                                       "org.freesmartphone.GSM.Monitor" )
            self._gsmNetworkIface = dbus.Interface( bus.get_object('org.freesmartphone.ogsmd', '/org/freesmartphone/GSM/Device'),
                                                       "org.freesmartphone.GSM.Network" )
            self._gsmCallIface = dbus.Interface( bus.get_object('org.freesmartphone.ogsmd', '/org/freesmartphone/GSM/Device'),
                                                       "org.freesmartphone.GSM.Call" )

    def get_hex (self, number):
        """ gets a number (which is probably hex string, but might be integer already) and returns it as integer
        """
        result = 0
        try:
            if type(number) == dbus.Int32:
                result = number
            else:
                result = int(number, 16)
                
        except Exception, e:
            logging.error('exception converting hex number: %s, input type is %s' % (str(e), type(number)))

        return result
        
    
    def call_status_handler(self, data, *args, **kwargs):
        """This maps to org.freesmartphone.GSM.Call.CallStatus.
        """
        logging.debug('Call status change notified, gets the lock.')
        self.acquire_lock()
        # CallStatus ( isa{sv} )
        #i: id
        #The index of the call that changed its status or properties.
        #s: status
        #The new status of the call. Expected values are:
        # * "incoming" = The call is incoming (but not yet accepted),
        # * "outgoing" = The call is outgoing (but not yet established),
        # * "active" = The call is the active call (you can talk),
        # * "held" = The call is being held,
        # * "release" = The call has been released.
        self._call_ongoing = False
        list = self._gsmCallIface.ListCalls()
        for call in list:
            index, status, properties = call
            if status != 'release':
                logging.info('Call ongoing: %i, %s.' % (index, status) )
                self._call_ongoing = True
        if not self._call_ongoing:
            logging.info('No call ongoing left.')
        logging.debug('Call status updated, released the lock.')
        self.release_lock()

    def call_ongoing(self):
        """Returns True if a call is ongoing. False otherwise."""
        logging.debug('call_ongoing() gets the lock.')
        self.acquire_lock()
        result = self._call_ongoing
        logging.debug('call_ongoing()? %s' % result)
        logging.debug('call_ongoing(), released the lock.')
        self.release_lock()
        return result

    def network_status_handler(self, data, *args, **kwargs):
        """Handler for org.freesmartphone.GSM.Network.Status signal.
        
        MCC, MNC, lac, cid and signal strengh are received asynchronuously through this signal/handler.
        Warning: we do not receive this signal when only the signal strength changes, see
        org.freesmartphone.GSM.Network.SignalStrength signal, and self.signal_strength_handler().
        """
        logging.debug("Wait for updating GSM data.")
        self.acquire_lock()
        logging.debug("Lock acquired, updating GSM data.")
        try:
            if data['registration'] == 'home' or data['registration'] == 'roaming':
                logging.info('Registration status is: %s.' % data['registration'])
            else:
                logging.info('Registration status is: %s. Skip.' % data['registration'])
                raise Exception, 'GSM data not available.'
                    
            if "lac" and "cid" and "strength" and "code" and "act" in data:
                self._MCC = (str(data['code'])[:3]).lstrip('0')
                self._MNC = (str(data['code'])[3:]).lstrip('0')
                self._networkAccessType = data['act']
                # lac and cid are hexadecimal strings
                self._lac = str(self.get_hex(data["lac"]))
                self._cid = str(self.get_hex(data["cid"]))
                # The signal strength in percent (0-100) is returned.
                # Mickey pointed out (see dev mailing list archive):
                # in module ogsmd.gsm.const:
                #def signalQualityToPercentage( signal ):
                #"""
                #Returns a percentage depending on a signal quality strength.
                #"""
                #<snip>
                #if signal == 0 or signal > 31:
                #    return 0
                #else:
                #    return int( round( math.log( signal ) / math.log( 31 ) * 100 ) )
                if data["strength"] == 0:
                    raise Exception, 'GSM strength (0) not suitable.'
                else:
                    self._strength = self.signal_percent_to_dbm( data["strength"] )
                    val_from_modem = (self._strength + 113 ) / 2
                    self._registrationGSM = data['registration']
                    logging.info("MCC %s MNC %s LAC %s, CID %s, strength %i/%i/%i (dBm, modem, percent 0-100)" % \
                                 (self._MCC, self._MNC, self._lac, self._cid,
                                  self._strength, val_from_modem, data['strength']))
                self.remember_serving_cell_as_seen(self._current_remember_cells_structure_id,
                                                   [{'lac':self._lac,
                                                     'cid':self._cid}]
                                                   )
            else:
                raise Exception, 'One or more required GSM data (MCC, MNC, lac, cid or strength) is missing.'
        except Exception, e:
            logging.warning('Unable to get GSM data (%s).' % str(e))
            self.empty_GSM_data()
        self.release_lock()
        logging.debug("GSM data updated, lock released.")
        self.notify_observers()

    def signal_strength_handler(self, data, *args, **kwargs):
        """Handler for org.freesmartphone.GSM.Network.SignalStrength signal.
        """
        logging.debug("Wait for updating GSM signal strength.")
        self.acquire_lock()
        logging.debug("Lock acquired, updating GSM signal strength.")
        try:
            new_dbm = self.signal_percent_to_dbm(data)
            if self.check_GSM():
                logging.info('GSM Signal strength updated from %i dBm to %i dBm (%i %%)' %
                             (self._strength,
                              new_dbm,
                              data))
                self._strength = new_dbm
            else:
                logging.info('GSM data invalid, no signal strength update to %i dBm (%i %%)' %
                             (new_dbm, data))
        except Exception, e:
            logging.warning('Unable to update GSM signal strength (%s).' % str(e))
        self.release_lock()
        logging.debug("GSM signal strength update finished, lock released.")
        self.notify_observers()

    def empty_GSM_data(self):
        """Empty all the local GSM related variables."""
        self._lac = ''
        self._cid = ''
        self._MCC = ''
        self._MNC = ''
        self._strength = 0
        self._registrationGSM = ''
        self._networkAccessType = ''
    
    def get_device_info(self):
        """If available, returns the manufacturer, model and revision."""
        #TODO call the dBus interface only if instance attributes are not set.
        try:
            obj = dbus.SystemBus().get_object('org.freesmartphone.ogsmd', '/org/freesmartphone/GSM/Device')
            data = dbus.Interface(obj, 'org.freesmartphone.Info').GetInfo()
        except Exception, e1:
            # API has changed. We try the old location, in case this client
            # runs on an older API system
            logging.error(e1)
            logging.info("Try the old GetInfo API")
            try:
                data = dbus.Interface(obj, 'org.freesmartphone.GSM.Device').GetInfo()
            except Exception, e2:
                logging.error(e2)
                data = []

        if 'manufacturer' in data:
            # At the moment the returned string starts and ends with '"' for model and revision
            self._manufacturer = data['manufacturer'].strip('"')
        if 'model' in data:
            # At the moment the returned string starts and ends with '"' for model and revision
            self._model = data['model'].strip('"')
        if 'revision' in data:
            # At the moment the returned string starts and ends with '"' for model and revision
            self._revision = data['revision'].strip('"')
        logging.info('Hardware manufacturer=%s, model=%s, revision=%s.' % \
                     (self._manufacturer, self._model, self._revision))
        return(self._manufacturer, self._model, self._revision)
        
    def check_GSM(self):
        """Returns True if valid GSM data is available."""
        # if something went wrong with GSM data then strength will be set to 0 (see empty_GSM_data() )
        # see 3GPP documentation TS 07.07 Chapter 8.5, GSM 07.07 command +CSQ
        return (self._strength >= -113 and self._strength <= -51)
    
    def signal_percent_to_dbm(self, val):
        """Translate the signal percent value to dbm."""
        # The signal strength in percent (0-100) is returned.
        # Mickey pointed out (see dev mailing list archive):
        # in module ogsmd.gsm.const:
        #def signalQualityToPercentage( signal ):
        #"""
        #Returns a percentage depending on a signal quality strength.
        #"""
        #<snip>
        #if signal == 0 or signal > 31:
        #    return 0
        #else:
        #    return int( round( math.log( signal ) / math.log( 31 ) * 100 ) )
        val_from_modem = int( round(math.exp(val * math.log( 31 ) /100)) )
        # translate to dBm (see 3GPP documentation TS 07.07 Chapter 8.5, GSM 07.07 command +CSQ)
        return val_from_modem * 2 - 113
    
    def get_serving_cell_information(self):
        """Returns a dictionary with serving cell monitoring data.
        
        If available contains 'lac' and 'cid'. May contain 'rxlev' and 'tav'.
        Otherwise returns an empty dictionary.
        Maximal timeout is 0.8 second.
        """
        result = {}
        try:
            data = self._gsmMonitoringIface.GetServingCellInformation(timeout = 0.8)
            
            # Debug
            # string hex
            #data['cid'] = '0'
            # string hex
            #data['lac'] = '0'
            # int
            #data['rxlev'] = 0
            #del data['lac']
            #del data['cid']
            # end of debug
            
            logging.debug( 'Raw data serving cell: %s' % data)
            
            if 'cid' in data and self.get_hex(data['cid']) == 0:
                # I have seen cid of 0. This does not make sense?
                logging.info('Serving cell with cell id of 0 discarded.')
            elif 'lac' in data and self.get_hex(data['lac']) == 0:
                # Not sure if I have seen lac of 0. This does not make sense? In case of...
                logging.info('Serving cell with lac of 0 discarded.')
            elif ('rxlev' in data) and (data['rxlev'] == 0):
                    logging.info('GSM rxlev (0) not suitable, this serving cell is discarded.')
            else:
                # wiki.openmoko.org/wiki/Neo_1973_and_Neo_FreeRunner_gsm_modem#Serving_Cell_Information_.282.2C1.29
                # states:
                # rxlev      Received Field Strength      (rxlev/2)+2 gives the AT+CSQ response value 
                # The best answer I could get was: no idea if this is correct.
                # Thus we keep the values as unmodified as possible.
                for key in ['rxlev', 'tav']:
                    if key in data:
                        result[key] = data[key]

                if "lac" in data and "cid" in data:
                    # lac and cid are hexadecimal strings
                    result['lac'] = str(self.get_hex(data["lac"]))
                    result['cid'] = str(self.get_hex(data["cid"]))
                else:
                    logging.warning('Either lac or cid is missing in serving cell information.')
                    result.clear()

        except Exception, e:
            logging.error('get serving cell info: %s' % str(e))
            result.clear()

        logging.debug( 'serving cell result: %s' % result)
        return result

    def get_neighbour_cell_info(self):
        """Returns a tuple of dictionaries, one for each cell.
        
        Each dictionary contains lac and cid fields.
        They may contain rxlev, c1, c2, and ctype.
        Maximal timeout is 0.8 second.
        """
        results = []
        try:
            data = self._gsmMonitoringIface.GetNeighbourCellInformation(timeout = 0.8)
            for cell in data:
                #logging.debug( 'Raw data neighbour cell: %s' % cell)
                if "lac" and "cid" in cell:
                    # lac and cid are hexadecimal strings
                    result = {}
                    result['lac'] = str(self.get_hex(cell["lac"]))
                    result['cid'] = str(self.get_hex(cell["cid"]))
                    # The signal strength in percent (0-100) is returned.
                    # The following comments were about the signal strength (see GetStatus):
                        # Mickey pointed out (see dev mailing list archive):
                        # in module ogsmd.gsm.const:
                        #def signalQualityToPercentage( signal ):
                        #"""
                        #Returns a percentage depending on a signal quality strength.
                        #"""
                        #<snip>
                        #if signal == 0 or signal > 31:
                        #    return 0
                        #else:
                        #    return int( round( math.log( signal ) / math.log( 31 ) * 100 ) )
                        
                        # http://wiki.openmoko.org/wiki/Neo_1973_and_Neo_FreeRunner_gsm_modem#Serving_Cell_Information_.282.2C1.29
                        # states:
                        # rxlev      Received Field Strength      (rxlev/2)+2 gives the AT+CSQ response value 
                        # The best answer I could get was: no idea if this is correct.
                        # Thus we keep the values as unmodified as possible.
                    if 'rxlev' in cell:
                        result['rxlev'] = cell['rxlev']
                    if 'c1' in cell:
                        result['c1'] = cell['c1']
                    if 'c2' in cell:
                        result['c2'] = cell['c2']
                    #if 'ctype' in cell:
                    #    result['ctype'] = ('NA', 'GSM', 'GPRS')[cell['ctype']]
                    #logging.debug( 'Neighbour cell result: %s' % result)
                    
                    if int(result['cid']) == 0:
                        # I have seen cid of 0. This does not make sense?
                        logging.info('Neighbour cell with cell id of 0 discarded.')
                    elif int(result['lac']) == 0:
                        # Not sure if I have seen lac of 0. This does not make sense? In case of...
                        logging.info('Neighbour cell with lac of 0 discarded.')
                    elif ('rxlev' in cell) and (cell['rxlev'] == 0):
                            logging.info('GSM rxlev (0) not suitable, this neighbour cell is discarded.')
                    else:
                        results.append(result)
        except Exception, e:
            logging.error('get neighbour cells info: %s' % str(e))
            return ()
        return tuple(results)
        
    def get_gsm_data(self):
        """Return validity boolean, tuple serving cell data, tuple of neighbour cells dictionaries.
        
        Operation is atomic, values cannot be modified while reading it.
        The validity boolean is True when all fields are valid and consistent,
        False otherwise.
        
        Serving cell tuple contains: MCC, MNC, lac, cid, signal strength, access type, timing advance, rxlev.
        Timing advance and rxlev may be emtpy.
        
        Each neighbour cell dictionary contains lac and cid fields.
        They may contain rxlev, c1, c2, and ctype.
        """
        logging.debug("Wait for reading GSM data.")
        self.acquire_lock()
        logging.debug("Lock acquired, reading GSM data.")
        (valid, mcc, mnc, lac, cid, strength, act) = (self.check_GSM(),
                                                      self._MCC,
                                                      self._MNC,
                                                      self._lac,
                                                      self._cid,
                                                      self._strength,
                                                      self._networkAccessType)
        neighbourCells = ()
        tav = ''
        rxlev = ''
        # this is deactivated for release 0.2.0
        # and re-activated for release 0.3.0
        if valid: 
            neighbourCells = self.get_neighbour_cell_info()
            servingInfo = self.get_serving_cell_information()
            # in case of a change in registration not already taken into account here
            # by processing D-Bus signal by network_status_handler(), we prefer using data
            # from get_serving_cell_information()
            if ('lac' in servingInfo) and ('cid' in servingInfo):
                lac = servingInfo['lac']
                cid = servingInfo['cid']
            
                # deactivated. Timing advance only works for the serving cell, and when a channel is actually open
                #if 'tav' in servingInfo:
                #    tav = str(servingInfo['tav'])
                    
                if 'rxlev' in servingInfo:
                    rxlev = str(servingInfo['rxlev'])

            self.remember_neighbour_cells_as_seen(self._current_remember_cells_structure_id,
                                                  neighbourCells)

        logging.info("valid=%s, MCC=%s, MNC=%s, lac=%s, cid=%s, strength=%s, act=%s, tav=%s, rxlev=%s" %
             (valid, mcc, mnc, lac, cid, strength, act, tav, rxlev))

        self.release_lock()
        logging.debug("GSM data read, lock released.")
        return (valid, (mcc, mnc, lac, cid, strength, act, tav, rxlev), neighbourCells )
    
    def get_status(self):
        """Get GSM status.
        
        Maps to org.freesmartphone.GSM.Network.GetStatus().
        It uses network_status_handler() to parse the output.
        """
        status = self._gsmNetworkIface.GetStatus()
        self.network_status_handler(status)

    def create_remember_cells_structure (self, id):
        """Tries to create a remember cells structure with given id.

        Throws exception if id already exists.
        Returns id on success."""
        if id in self._remember_cells_structures:
            raise Exception, 'create_remember_cells_strucutre(): id already exists.'
        else:
            self._remember_cells_structures[id] = {}
            logging.info('id:%s added to remember cells structure.' % id)
            return id

    def remember_cells_as_seen(self, id, cells, type):
        """Parses cells dictionaries list and remembers having seen it.

        id: id of the remember cells structure to act upon
        cells: a list of dictionaries describing cells to remember
        type: 0 : servings, 1 : neighbours
        Cells already seen are ignored.
        If the id is not the one from the structure used to remember all the cells,
        and a new value is added, it calls the method on the "total" structure."""

        if not type in (0, 1):
            raise Exception, 'remember_cells_as_seen() wrong type value (%s).' % type

        structure = self._remember_cells_structures

        if not id in structure:
            logging.warning('Remember_cells_as_seen(): id (%s) cannot be found.' % id)
            return
        else:
            structure = self._remember_cells_structures[id]

        if self._MCC == '':
            logging.debug('remember_cells_as_seen(): ignores empty MCC.')
            return
        elif not self._MCC in structure:
            logging.info('Adds MCC:%s to remember cell structure id (%s).' % (self._MCC, id))
            structure[self._MCC] = {}

        structure = structure[self._MCC]

        if self._MNC == '':
            logging.debug('remember_cells_as_seen(): ignores empty MNC.')
            return
        elif not self._MNC in structure:
            logging.info('Adds MNC:%s to remember cell structure id (%s), MCC:%s.' %
                         (self._MNC, id, self._MCC))
            # servings and neighbours
            structure[self._MNC] = ({}, {})

        structure = structure[self._MNC][type]
        logging.info('Update remember cells structure for %s.' % ['servings', 'neighbours'][type])

        for cell in cells:
            if cell['lac'] in structure:
                if cell['cid'] in structure[ cell['lac'] ]:
                    logging.debug('Cell %(lac)s / %(cid)s has already been seen' % cell)
                    continue
                else:
                    structure[ cell['lac'] ].append(cell['cid'])
                    logging.info('Cell %(lac)s / %(cid)s adds a new cid to remember' % cell +
                                 ' to structure id: %s' % id)
            else:
                structure[ cell['lac'] ] = [ cell['cid'] ]
                logging.info('Cell %(lac)s / %(cid)s adds new lac and cid to remember' % cell +
                             ' to structure id: %s' % id)
            self.increase_remember_cells_stats(id, type)
            if id != self.REMEMBER_CELLS_STRUCTURE_TOTAL_ID:
                    # we are not in the structure which gathers *all* cells seen
                    logging.debug('Try updating remember cells total structure.')
                    self.remember_cells_as_seen(self.REMEMBER_CELLS_STRUCTURE_TOTAL_ID,
                                                [cell],
                                                type)

    def increase_remember_cells_stats(self, id, type):
        """Increments the number of cells for the id and type.

        id: id of the remember cells structure to act upon
        type: 0 : servings, 1 : neighbours
        """

        if not type in (0, 1):
            raise Exception, 'increase_remember_cells_stats() wrong type value (%s).' % type

        if id == self.REMEMBER_CELLS_STRUCTURE_TOTAL_ID:
            if type == 0:
                self._nb_remember_servings_total += 1
            else:
                self._nb_remember_neighbours_total += 1
            logging.debug('Total numbers of cells seen are now: %i, %i.' %
                          (self._nb_remember_servings_total,
                           self._nb_remember_neighbours_total)
                          )
        else:
            if type == 0:
                self._nb_remember_servings_in_last_structure += 1
            else:
                self._nb_remember_neighbours_in_last_structure += 1
            logging.debug('Numbers of cells seen in last structure are now: %i, %i.' %
                          (self._nb_remember_servings_in_last_structure,
                           self._nb_remember_neighbours_in_last_structure)
                          )

    def remember_serving_cell_as_seen(self, id, serving):
        """Remembers having seen the serving cell.

        id: id of the remember cells structure to act upon
        serving: a list of dictionaries describing cells to remember
        """
        self.remember_cells_as_seen(id, serving, 0)

    def remember_neighbour_cells_as_seen(self, id, neighbours):
        """Remembers having seen the neighbour cells.

        id: id of the remember cells structure to act upon
        neighbours: a list of dictionaries describing cells to remember
        """
        self.remember_cells_as_seen(id, neighbours, 1)

    def set_current_remember_cells_id(self, id):
        """Sets the current remember cells structure id, creates it if necessary."""

        if not id:
            logging.error('id:\'%s\' is not valid for a remember cells structure.' % id)
            return
        
        logging.debug("Wait for GSM Lock data.")
        self.acquire_lock()
        logging.debug("Lock acquired by set_current_remember_cells_id().")

        if id in self._remember_cells_structures:
            logging.info('id:\'%s\' already existed in remember cells structure.' % id)
        else:
            self.create_remember_cells_structure(id)

        self._current_remember_cells_structure_id = id
        #TODO: you can set an existing id. In that case, you should initialise
        # the values with the content of the existing structure
        self._nb_remember_servings_in_last_structure = 0
        self._nb_remember_neighbours_in_last_structure = 0
        logging.debug('Remember cells counters for current structure reset to 0.')
        logging.info('current remember cells structure id set to: %s' % id)

        self.release_lock()
        logging.debug("Lock released by set_current_remember_cells_id().")

    def get_seen_cells_stats(self):
        """Returns the number of cells which have been seen.

        Returns a tuple:
        number of serving cells seen in last remember structure,
        number of neighbour cells seen in last remember structure,
        number of serving cells seen since launch,
        number of neighbour cells seen since launch
        """
        logging.debug("Wait for GSM Lock data.")
        self.acquire_lock()
        logging.debug("Lock acquired by get_seen_cells_stats(), reading.")
        res = (self._nb_remember_servings_in_last_structure,
                self._nb_remember_neighbours_in_last_structure,
                self._nb_remember_servings_total,
                self._nb_remember_neighbours_total)
        self.release_lock()
        logging.debug("Lock released by get_seen_cells_stats().")
        return res

    def acquire_lock(self):
        """Acquire the lock to prevent state of the GSM variables to be modified."""
        self.lock.acquire()
        
    def release_lock(self):
        """Release the lock on the object"""
        self.lock.release()
        
    def notify_observers(self):
        for obs in self._observers:
            obs.notify()
        logging.debug('Gsm class notifies its observers.')
            
    def register(self, observer):
        self._observers.append(observer)
    
class Config:

    def __init__(self, config_filename):
        self._configuration_filename = config_filename
        self._config = self.load_config()
                
    def load_config(self):
        """Try loading the configuration file.
        
        Try to load the configuration file. If it does not exist, the default values
        are loaded, and the configuration file is saved with these default values.
        """
        logging.debug('Loading configuration file: \'%s\'' % self._configuration_filename)
        config = ConfigParser.RawConfigParser();
        try:
            config.readfp(open(self._configuration_filename))
            logging.debug('Configuration file loaded.')
        except Exception, e:
                logging.warning("No configuration file found.")
        return config

    def get(self, section, option):
        return self._config.get(section, option)

    def getint(self, section, option):
        return self._config.getint(section, option)

    def set(self, section, option, value):
        self._config.set(section, option, value)

    def set_config_if_not_exist(self, params):
        """For every value in params, insert it only if it is not already present.

        params is a [ (section, [ (option, value) ] ) ].
        It saves the config file, if a modification happened.
        """
        neededToSave = False
        
        for s in params:
            section, tuples = s
            for t in tuples:
                if not self._config.has_section(section):
                    logging.info("Adding section '%s' to config file." % section)
                    self._config.add_section(section)
                if not self._config.has_option(section, t[0]):
                    logging.info("Adding to section '%s' the option '%s' with value '%s' to config file."
                                 % ( (section,) + t))
                    self.set(section, *t)
                    neededToSave = True
        if neededToSave:
            self.save_config()

    def save_config(self):
        configFile = open(self._configuration_filename, 'wb')
        logging.info('Save config file \'%s\'' % self._configuration_filename)
        self._config.write(configFile)
        configFile.close()        
    
class Gps:
    
    GYPSY_DEVICE_FIX_STATUS_INVALID = 0
    GYPSY_DEVICE_FIX_STATUS_NONE = 1
    # A fix with latitude and longitude has been obtained 
    GYPSY_DEVICE_FIX_STATUS_2D = 2
    # A fix with latitude, longitude and altitude has been obtained
    GYPSY_DEVICE_FIX_STATUS_3D = 3

    def __init__(self):	
        self._dbusobj = dbus.SystemBus().get_object('org.freesmartphone.ogpsd', '/org/freedesktop/Gypsy')
        self._lat = -1
        self._lng = -1
        self._alt = -1
        self._spe = -1
        self._user_speed_kmh = -1
        self._pdop = -1
        self._hdop = -1
        self._vdop = -1
        self._tstamp = -1

    def request(self):
        """Requests the GPS resource through /org/freesmartphone/Usage."""
        obj = dbus.SystemBus().get_object('org.freesmartphone.ousaged', '/org/freesmartphone/Usage')
        request = dbus.Interface(obj, 'org.freesmartphone.Usage').RequestResource('GPS')
        if (request == None):
            logging.info("GPS resource succesfully requested (%s)." % request)
            return True
        else:
            logging.critical("ERROR requesting the GPS (%s)" % request)
            return False

    def release(self):
        """Releases the GPS resource through /org/freesmartphone/Usage."""
        obj = dbus.SystemBus().get_object('org.freesmartphone.ousaged', '/org/freesmartphone/Usage')
        release = dbus.Interface(obj, 'org.freesmartphone.Usage').ReleaseResource('GPS')
        if (release == None):
            logging.info("GPS resource succesfully released (%s)." % release)
            return True
        else:
            logging.info("ERROR releasing the GPS (%s)" % release)
            return False
    
    def get_GPS_data(self):
        """Returns Validity boolean, time stamp, lat, lng, alt, pdop, hdop, vdop."""
        logging.debug('Get GPS position')
        (fields, tstamp, lat, lng, alt) = dbus.Interface(self._dbusobj, 'org.freedesktop.Gypsy.Position').GetPosition()
        # From Python doc: The precision determines the number of digits after the decimal point and defaults to 6.
        # A difference of the sixth digit in lat/long leads to a difference of under a meter of precision.
        # Thus 6 is good enough.
        logging.debug('GPS position: fields (%d), lat (%f), lnt (%f), alt (%f)'
                      % (fields, lat, lng, alt))
        valid = True
        if fields != 7:
            valid = False
        (fields, pdop, hdop, vdop) = self._dbusobj.GetAccuracy(dbus_interface='org.freedesktop.Gypsy.Accuracy')
        logging.debug('GPS accuracy: fields (%d), pdop (%g), hdop (%g), vdop (%g)'
                      % (fields, pdop, hdop, vdop))
        if fields != 7:
            valid = False
        self._lat = lat
        self._lng = lng
        self._alt = alt
        self._pdop = pdop
        self._hdop = hdop
        self._vdop = vdop
        self._tstamp = tstamp
        return valid, tstamp, lat, lng, alt, pdop, hdop, vdop
        
    def get_course(self):
        """Return validity boolean, speed in knots, heading in decimal degree."""
        (fields, tstamp, speed, heading, climb) = self._dbusobj.GetCourse(dbus_interface='org.freedesktop.Gypsy.Course')
        logging.debug('GPS course: fields (%d), speed (%f), heading (%f)'
                      % (fields, speed, heading))
        if (fields & (1 << 0)) and (fields & (1 << 1)):
            return True, speed, heading
        return False, speed, heading


class ObmLogger():
    # Lock to access the OBM logs files
    fileToSendLock = threading.Lock()
    APP_HOME_DIR = os.path.join(os.environ['HOME'], '.openBmap')
    TEMP_LOG_FILENAME = os.path.join(APP_HOME_DIR,
                                     'openBmap.log')
    CONFIGURATION_FILENAME = os.path.join(APP_HOME_DIR,
                                          'openBmap.conf')
    PLUGINS_RELATIVE_PATH = "plugins"

    def __init__(self):
        self.XML_LOG_VERSION = 'V2'
        # For ease of comparison in database, we use ##.##.## format for version:
        self.SOFTWARE_VERSION = '0.4.1'
        # strings which will be used in the configuration file
        self.GENERAL = 'General'
        self.OBM_LOGS_DIR_NAME = 'OpenBmap logs directory name'
        self.OBM_PROCESSED_LOGS_DIR_NAME = 'OpenBmap uploaded logs directory name'
        self.OBM_UPLOAD_URL = 'OpenBmap upload URL'
        self.OBM_API_CHECK_URL = 'OpenBmap API check URL'
        self.OBM_API_VERSION = 'OpenBmap API version'
        self.SCAN_SPEED_DEFAULT = 'OpenBmap logger default scanning speed (in sec.)'
        self.MIN_SPEED_FOR_LOGGING = 'GPS minimal speed for logging (km/h)'
        self.MAX_SPEED_FOR_LOGGING = 'GPS maximal speed for logging (km/h)'
        # NB_OF_LOGS_PER_FILE is considered for writing of log to disk only if MAX_LOGS_FILE_SIZE <= 0
        self.NB_OF_LOGS_PER_FILE = 'Number of logs per file'
        # puts sth <=0 to MAX_LOGS_FILE_SIZE to ignore it and let other conditions trigger
        # the write of the log to disk (e.g. NB_OF_LOGS_PER_FILE)
        self.MAX_LOGS_FILE_SIZE = 'Maximal size of log files to be uploaded (kbytes)'
        self.APP_LOGGING_LEVEL = 'Application logging level (debug, info, warning, error, critical)'
        self.LIST_OF_ACTIVE_PLUGINS = 'List of active plugins (try to load them at startup)'

        self.CREDENTIALS = 'Credentials'
        self.OBM_LOGIN = 'OpenBmap login'
        self.OBM_PASSWORD = 'OpenBmap password'

        # set default values if necessary
        config.set_config_if_not_exist([
                                        (self.GENERAL,[
                                                       (self.OBM_LOGS_DIR_NAME,
                                                        os.path.join(self.APP_HOME_DIR, 'Logs')),
                                                        (self.OBM_PROCESSED_LOGS_DIR_NAME,
                                                         os.path.join(self.APP_HOME_DIR, 'Processed_logs')),
                                                        (self.OBM_UPLOAD_URL,
                                                         'http://openbmap.org/upload/upl.php5'),
                                                        (self.OBM_API_CHECK_URL,
                                                         'http://openbmap.org/getInterfacesVersion.php5'),
                                                        (self.OBM_API_VERSION,
                                                         '2'),
                                                        (self.SCAN_SPEED_DEFAULT,
                                                         10), # in sec.
                                                        (self.MIN_SPEED_FOR_LOGGING,
                                                         0),
                                                        (self.MAX_SPEED_FOR_LOGGING,
                                                         150),
                                                        (self.NB_OF_LOGS_PER_FILE,
                                                         3),
                                                        (self.MAX_LOGS_FILE_SIZE,
                                                         20),
                                                        (self.APP_LOGGING_LEVEL,
                                                         'info'),
                                                        (self.LIST_OF_ACTIVE_PLUGINS,
                                                         [])
                                                       ]),
                                        (self.CREDENTIALS, [
                                                            (self.OBM_LOGIN,
                                                             'your_login'),
                                                            (self.OBM_PASSWORD,
                                                             'your_password')
                                                            ])
                                        ])
        if not self.validate_configuration():
            errMsg = "Configuration file could not be validated. See logs for details. Exiting..."
            logging.critical(errMsg)
            print errMsg
            #TODO: well in case this happens, this should be forwarded to Views (GUI) in order to inform the user
            sys.exit(-1)
        else:
            logging.info("Configuration file entries for ObmLogger validated.")

        if not os.path.exists(ObmLogger.APP_HOME_DIR):
            print('Main directory does not exists, creating \'%s\'' %
                  ObmLogger.APP_HOME_DIR)
            os.mkdir(ObmLogger.APP_HOME_DIR)

        logging.info("Requesting GPS...")
        self._gps = Gps()
        # start GPS ASAP, to give it time to get fix
        #logging.info("Will now request GPS resource...")
        self._gps.request()
        self._observers = []

        # Try getting the list of active plugins objects, to be used for scheduling, etc.
        self._activePluginsList = self.load_active_plugins()

        # is currently logging? Used to tell the thread to stop
        self._logging = False
        self._loggingThread = None
        # This is a list of source ids, such as returned by gobject.idle_add(),
        # gobject.timeout_add()
        self._activePluginScheduledIds = []

        self._bus = self.init_dbus()
        self._gsm = Gsm(self._bus)
        self._gsm.register(self)
        self._mcc = ""
        self._loggerLock = threading.Lock()
        # we will store every log in this list, until writing it to a file:
        self._logsInMemory = []
        # _logsInMemory is a list of strings, which will be concatenated to write to disk
        self._logsInMemoryLengthInByte = 0
        self._logFileHeader = "<?xml version=\"1.0\" encoding=\"UTF-8\"?>\n" + \
        "<logfile manufacturer=\"%s\" model=\"%s\" revision=\"%s\" swid=\"FSOnen1\" swver=\"%s\">\n" \
        % ( self._gsm.get_device_info() + (self.SOFTWARE_VERSION,) )
        self._logFileTail = '</logfile>'
        
        logLvl = self.get_config_value(self.GENERAL, self.APP_LOGGING_LEVEL)
        logLvl = logging.__dict__[logLvl]
        logging.getLogger().setLevel(logLvl)
        logging.info('Application logging level set to %s' % logging.getLevelName(logLvl))

        # DEBUG = True if you want to activate GPS/Web connection simulation
        self.DEBUG = False
        if self.DEBUG:
            self.get_gps_data = self.simulate_gps_data
            #self.get_gsm_data = self.simulate_gsm_data

    def validate_configuration(self):
        """Validates the config values. Returns True uppon success."""
        section = self.GENERAL

        for option in [self.SCAN_SPEED_DEFAULT,
                      self.MIN_SPEED_FOR_LOGGING,
                      self.MAX_SPEED_FOR_LOGGING,
                      self.NB_OF_LOGS_PER_FILE,
                      self.MAX_LOGS_FILE_SIZE]:
            try:
                self.get_config_value(section, option)
            except Exception, e:
                logging.error('Validation of configuration failed for (%s, %s): %s. It should be an integer.' %
                              (section, option, str(e)) )
                return False

        try:
            res = self.get_config_value(section, self.APP_LOGGING_LEVEL)
            if not res in ['DEBUG', 'INFO', 'WARNING', 'ERROR', 'CRITICAL']:
                logging.error('Application logging level should be one of'\
                              ' DEBUG, INFO, WARNING, ERROR, CRITICAL.'\
                              ' Found: %s' % res)
                return False
        except Exception, e:
            logging.error('Validation of configuration failed for (%s, %s): %s.' %
                              (section, self.APP_LOGGING_LEVEL, str(e)) )
            return False

        return True

    def get_config_value(self, section, option):
        """Returns the value, under the expected form (string, int, etc.).

        See validate_configuration() method for expected values format.
        """
        if option in [self.SCAN_SPEED_DEFAULT,
                      self.MIN_SPEED_FOR_LOGGING,
                      self.MAX_SPEED_FOR_LOGGING,
                      self.NB_OF_LOGS_PER_FILE,
                      self.MAX_LOGS_FILE_SIZE]:
            return config.getint(section, option)

        elif option in [self.APP_LOGGING_LEVEL]:
            return str.upper(config.get(section, option))

        else:
            return config.get(section, option)

    def get_config(self):
        """Gets the config object used."""
        return config

    def request_resource(self, resource):
        """Requests the given string resource through /org/freesmartphone/Usage."""
        obj = self._bus.get_object('org.freesmartphone.ousaged', '/org/freesmartphone/Usage')
        request = dbus.Interface(obj, 'org.freesmartphone.Usage').RequestResource(resource)
        if (request == None):
            logging.info("'%s' resource succesfully requested (%s)." % (resource, request))
            return True
        else:
            logging.critical("ERROR requesting the resource '%s' (%s)" % (resource, request))
            return False
        
    def release_resource(self, resource):
        """Releases the given string resource through /org/freesmartphone/Usage."""
        obj = self._bus.get_object('org.freesmartphone.ousaged', '/org/freesmartphone/Usage')
        release = dbus.Interface(obj, 'org.freesmartphone.Usage').ReleaseResource(resource)
        if (release == None):
            logging.info("'%s' resource succesfully released (%s)." % (resource, release))
            return True
        else:
            logging.critical("ERROR releasing the resource '%s' (%s)" % (resource, release))
            return False
        
    def test_write_obm_log(self):
        self.write_obm_log(str(datetime.now()), 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12)
    
    def write_obm_log(self, date, tstamp, servingCell, lng, lat, alt, spe, heading, hdop, vdop, pdop, neighbourCells):
        """Format and stores in memory given data, possibly triggers writing in log file."""
        # log format, for release 0.2.0
        # "<gsm mcc=\"%s\" mnc=\"%s\" lac=\"%s\" id=\"%s\" ss=\"%i\"/>" % servingCell[:5]
        logmsg = "<scan time=\"%s\">" % date + \
        "<gsmserving mcc=\"%s\" mnc=\"%s\" lac=\"%s\" id=\"%s\" ss=\"%i\" act=\"%s\"" % servingCell[:6]
        if servingCell[6] != "":
            logmsg += " tav=\"%s\"" % servingCell[6]
        else:
            logging.debug("No timing advance available for serving cell, skip it.")
        if servingCell[7] != "":
            logmsg += " rxlev=\"%s\"" % servingCell[7]
        else:
            logging.debug("No rxlev available for serving cell, skip it.")
        logmsg += "/>"
         
        
        for cell in neighbourCells:
            # the best answer we could get was: it is highly probable that the neighbour cells have
            # the same MCC and MNC as the serving one, but this is not absolutely sure.
            logmsg += "<gsmneighbour mcc=\"%s\" mnc=\"%s\" lac=\"%s\"" % (servingCell[:2] + (cell['lac'],)) +\
            " id=\"%s\"" % cell['cid'] + \
            " rxlev=\"%i\"" % cell['rxlev'] + \
            " c1=\"%i\"" % cell['c1'] + \
            " c2=\"%i\"" % cell['c2'] + \
            "/>"
            #" ctype=\"%s\"" % cell['ctype'] + \

        logmsg += self.format_gps_data_for_xml_log(
                                                   (True,
                                                    tstamp,
                                                    lat,
                                                    lng,
                                                    alt,
                                                    pdop,
                                                    hdop,
                                                    vdop,
                                                    spe,
                                                    heading)
                                                   )
        logmsg += "</scan>\n"
        logging.info(logmsg)
        self.fileToSendLock.acquire()
        logging.info('OpenBmap log file lock acquired by write_obm_log.')
        #debug
        #self._gsm._call_ongoing = True
        #end of debug
        if self._gsm.call_ongoing():
            # see comments in log() about not logging in a call.
            logging.info('write_obm_log() canceled because a call is ongoing.')
            self.fileToSendLock.release()
            logging.info('OpenBmap log file lock released.')
            return

        maxLogsFileSize = self.get_config_value(self.GENERAL, self.MAX_LOGS_FILE_SIZE) * 1024

        if ( maxLogsFileSize > 0 ):
            # we use the max log file size as criterium to trigger write of file
            
            # we write ascii file, that is to say, one byte per character
            fileLengthInByte = len(self._logFileHeader) + len(logmsg) \
            + self._logsInMemoryLengthInByte + len(self._logFileTail)

            if (fileLengthInByte <= maxLogsFileSize):
                logging.debug('Current size of logs in memory %i bytes, max size of log files is %i bytes.'
                              % (fileLengthInByte, maxLogsFileSize))
            else:
                self.write_gsm_log_to_disk_unprotected()
            self._logsInMemory.append(logmsg)
            self._logsInMemoryLengthInByte += len(logmsg)
        else:
            self._logsInMemory.append(logmsg)
            self._logsInMemoryLengthInByte += len(logmsg)
            if len(self._logsInMemory) < self.get_config_value(self.GENERAL, self.NB_OF_LOGS_PER_FILE):
                logging.debug('Max logs per file (%i/%i) not reached, wait to write to a file.'
                              % (len(self._logsInMemory), self.get_config_value(self.GENERAL, self.NB_OF_LOGS_PER_FILE)))
            else:
                self.write_gsm_log_to_disk_unprotected()

        self.fileToSendLock.release()
        logging.info('OpenBmap log file lock released.')

    def format_gps_data_for_xml_log(self, gpsData):
        """Receives GPS data as parameter, returns an XML formated string for log file.

        gpsData follows the return type of get_gps_data():
        validity boolean, time stamp, lat, lng, alt, pdop, hdop, vdop, speed in km/h, heading."""

        # From Python doc: %f -> The precision determines the number of digits after the decimal point and defaults to 6.
        # A difference of the sixth digit in lat/long leads to a difference of under a meter of precision.
        # Maximum error by rounding is 5. GPS precision is at best 10m. 2 x (maxError x error) = 2 x (5 x 1)
        # introduces an error of 10m! Thus we settle to 9.
        latLonPrecision = 9
        # http://gpsd.berlios.de/gpsd.html:
        # Altitude determination is more sensitive to variability to atmospheric signal lag than latitude/longitude,
        # and is also subject to errors in the estimation of local mean sea level; base error is 12 meters at 66%
        # confidence, 23 meters at 95% confidence. Again, this will be multiplied by a vertical dilution of
        # precision (VDOP).
        # Altitude is in meter.
        altitudePrecision = 1
        # speed is in km/h.
        speedPrecision = 3
        # Precision of 2 digits after the decimal point for h/p/v-dop is enough.
        hvpdopPrecision = 2
        # heading in decimal degrees
        headingPrecision = 9

        return (
                "<gps time=\"%s\"" % time.strftime('%Y%m%d%H%M%S', time.gmtime(gpsData[1])) + \
                " lng=\"%s\"" % ( ('%.*f' % (latLonPrecision, gpsData[3])).rstrip('0').rstrip('.') ) + \
                " lat=\"%s\"" % ( ('%.*f' % (latLonPrecision, gpsData[2])).rstrip('0').rstrip('.') ) + \
                " alt=\"%s\"" % ( ('%.*f' % (altitudePrecision, gpsData[4])).rstrip('0').rstrip('.') ) + \
                " hdg=\"%s\"" % ( ('%.*f' % (headingPrecision, gpsData[9])).rstrip('0').rstrip('.') ) + \
                " spe=\"%s\"" % ( ('%.*f' % (speedPrecision, gpsData[8])).rstrip('0').rstrip('.') ) + \
                " hdop=\"%s\"" % ( ('%.*f' % (hvpdopPrecision, gpsData[6])).rstrip('0').rstrip('.') ) + \
                " vdop=\"%s\"" % ( ('%.*f' % (hvpdopPrecision, gpsData[7])).rstrip('0').rstrip('.') ) + \
                " pdop=\"%s\"" % ( ('%.*f' % (hvpdopPrecision, gpsData[5])).rstrip('0').rstrip('.') ) + \
                "/>"
                )

    def write_obm_log_to_disk(self):
        """Gets the Lock and then calls write_gsm_log_to_disk_unprotected()."""
        self.fileToSendLock.acquire()
        logging.info('OpenBmap log file lock acquired by write_obm_log_to_disk().')
        self.write_gsm_log_to_disk_unprotected()
        self.fileToSendLock.release()
        logging.info('OpenBmap log file lock released by write_obm_log_to_disk().')

    def write_gsm_log_to_disk_unprotected(self):
        """Takes the logs already formatted in memory and write them to disk. Clears the log in memory.

        Warning: this method is not protected by a Lock!
        """

        if len(self._logsInMemory) == 0:
            logging.info('No log to write to disk, returning.')
            return

        now = datetime.now()
        #"yyyyMMddHHmmss"
        date = now.strftime("%Y%m%d%H%M%S")

        logDir = self.get_config_value(self.GENERAL, self.OBM_LOGS_DIR_NAME)
        logDir = os.path.join(logDir, "FSO_GSM")
        if (not os.path.exists(logDir)):
            os.mkdir(logDir)

        # at the moment: log files follow: logYYYYMMDDhhmmss.xml
        # log format, for release 0.2.0
        # filename = os.path.join(logDir, 'log' + date + '.xml')
        # new filename format VX_MCC_logYYYYMMDDhhmmss.xml
        mcc = self._logsInMemory[0]
        # len('mcc="') = 5
        mcc = mcc[mcc.find("mcc=") + 5 : ]
        mcc = mcc[ : mcc.find('"')]
        filename = os.path.join(logDir, self.XML_LOG_VERSION + '_' + mcc + '_log' + date + '.xml')
        logmsg = self._logFileHeader
        for log in self._logsInMemory:
            logmsg += log
        #TODO: escaped characters wich would lead to malformed XML document (e.g. '"')
        logmsg += self._logFileTail
        logging.debug('Write logs to file: %s' % logmsg)
        try:
            file = open(filename, 'w')
            file.write(logmsg)
            file.close()
            self._logsInMemory[:] = []
            self._logsInMemoryLengthInByte = 0
        except Exception, e:
            logging.error("Error while writing GSM/GPS log to file: %s" % str(e))

    def write_generic_log_file_to_disk(self, plugin_name, file_name, content):
        """Generic method to write given content, into corresponding path and file.
        
        Corresponding path is: applicaton_log_dir/plugin_name/.
        """
        if ((content == None) or (len(content) <= 0)):
            logging.error("Tried writing an empty log to disk")
        elif ((file_name == None) or (len(file_name) <= 0)):
            logging.error("path and log filename are missing")
        else:
            logDir = self.get_config_value(self.GENERAL, self.OBM_LOGS_DIR_NAME)
            absolute_filename = os.path.join(logDir, plugin_name)
            absolute_filename = os.path.join(absolute_filename, file_name)
            try:
                targetFile = open(absolute_filename, 'w')
                targetFile.write(content)
                targetFile.close()
                logging.info("writen '%s' targetFile on disk", absolute_filename)
            except Exception, e:
                logging.error("Error while writing generic log targetFile to disk: %s" % str(e))

    def send_logs(self):
        """Try uploading available log files to OBM database.
        
        Returns (b, i, i):
        True if nothing wrong happened.
        The total number of successfully uploaded files.
        The total number of files available for upload.
        """
        totalFilesToUpload = 0
        totalFilesUploaded = 0
        result = True
        
        # to store the data once sent:
        dirProcessed = os.path.join(self.get_config_value(self.GENERAL, self.OBM_PROCESSED_LOGS_DIR_NAME))
        logsDir = self.get_config_value(self.GENERAL, self.OBM_LOGS_DIR_NAME)
        logsDir = os.path.join(logsDir, "FSO_GSM")
        
        self.fileToSendLock.acquire()
        logging.info('OpenBmap log file lock acquired by send_logs.')
        try:
            if not self.check_obm_api_version():
                logging.error('We do not support the server API version,' + \
                              'do you have the latest version of the software?')
                return (False, -1, -1)
            os.chdir(logsDir)
            for f in os.listdir(logsDir):
                totalFilesToUpload += 1
                logging.info('Try uploading \'%s\'' % f)
                fileRead = open(f, 'r')
                content = fileRead.read()
                fileRead.close()
                (status, reason, resRead) = Upload.post_url(self.get_config_value(self.GENERAL, self.OBM_UPLOAD_URL),
                                                            [('openBmap_login', self.get_config_value(self.CREDENTIALS, self.OBM_LOGIN)),
                                                            ('openBmap_passwd', self.get_config_value(self.CREDENTIALS, self.OBM_PASSWORD))],
                                                            [('file', f, content)])
                logging.debug('Upload response status:%s, reason:%s, body:%s' % (status, reason, resRead))
                if resRead.startswith('Stored in'):
                    newName = os.path.join(dirProcessed, f)
                    os.rename(f, newName)
                    logging.info('File \'%s\' successfully uploaded. Moved to \'%s\'. Thanks for contributing!' %
                                 (f, newName))
                    totalFilesUploaded += 1
                elif resRead.strip(' ').endswith('already exists.'):
                    # We assume the file has already been uploaded...
                    newName = os.path.join(dirProcessed, f)
                    os.rename(f, newName)
                    logging.info('File \'%s\' probably already uploaded. Moved to \'%s\'. Thanks for contributing!' %
                                 (f, newName))
                else:
                    logging.error('Unable to upload file \'%s\'. Err: %d/%s: %s' % (f, status, reason, resRead))
                    result = False
        except Exception, e:
            logging.error("Error while sending GSM/GPS logged data: %s" % str(e))
            return (False, totalFilesUploaded, totalFilesToUpload)
        finally:
            self.fileToSendLock.release()
            logging.info('OpenBmap log file lock released.')
        return (result, totalFilesUploaded, totalFilesToUpload)

    def delete_processed_logs(self):
        """Deletes all the files located in the 'processed' folder. Returns number deleted."""
        # no Lock used here, I don't see this needed for Processed logs...
        dirProcessed = os.path.join(self.get_config_value(self.GENERAL, self.OBM_PROCESSED_LOGS_DIR_NAME))
        deletedSoFar = 0
        for f in os.listdir(dirProcessed):
            toBeDeleted = os.path.join(dirProcessed, f)
            os.remove(toBeDeleted)
            deletedSoFar += 1
            logging.info('Processed log file \'%s\' has been deleted.' % toBeDeleted)
        return deletedSoFar
        
    def check_obm_api_version(self):
        """Get the current openBmap server API version, and return True if it corresponds."""
        logging.debug('Checking the openBmap server interface Version...')
        if self.DEBUG:
            # simulation
            return True
        try:
            logging.info('We support API version: %s.' % self.get_config_value(self.GENERAL, self.OBM_API_VERSION))
            response = urllib2.urlopen(self.get_config_value(self.GENERAL, self.OBM_API_CHECK_URL))
            for line in response:
                if line.startswith('MappingManagerVersion='):
                    version_string, val = line.split('=')
                    val = val.strip(' \n')
                    logging.info('Server API version: %s.' % val)
                    if  val == self.get_config_value(self.GENERAL, self.OBM_API_VERSION):
                        return True
        except Exception, e:
            logging.error(str(e))
        return False
        
    def init_dbus(self):
        """initialize dbus"""
        logging.debug("trying to get bus...")
        try:
            bus = dbus.SystemBus()
        except Exception, e:
            logging.error( "Can't connect to dbus: %s" % e )
        logging.debug("ok")
        return bus
    
    def init_openBmap(self):
        # this is intended to prevent the phone to go to suspend
        self.request_resource('CPU')
        
        logDir = self.get_config_value(self.GENERAL, self.OBM_LOGS_DIR_NAME)
        if not os.path.exists(logDir):
            logging.info('Directory for storing cell logs does not exists, creating \'%s\'' % 
                         logDir)
            os.mkdir(logDir)
                
        # to store the data once sent:
        dirProcessed = os.path.join(self.get_config_value(self.GENERAL, self.OBM_PROCESSED_LOGS_DIR_NAME))
        if not os.path.exists(dirProcessed):
            logging.info('Directory for storing processed cell logs does not exists, creating \'%s\'' % dirProcessed)
            os.mkdir(dirProcessed)
            
        # request the current status. If we are connected we get the data now. Otherwise
        # we would need to wait for a signal update.
        self._gsm.get_status()
        
        # check if we have no ongoing call...
        self._gsm.call_status_handler(None)

    def exit_openBmap(self):
        """Puts the logger in a nice state for exiting the application.

        * Saves logs in memory if any."""
        self.write_obm_log_to_disk()
        self._gps.release()
        self.release_resource('CPU')

    def load_active_plugins(self):
        """Tries loading active plugins. Returns a list of successfully loaded pluging."""
        result = []
        pluginsNames = eval(str(self.get_config_value(self.GENERAL, self.LIST_OF_ACTIVE_PLUGINS)))

        for pluginName in pluginsNames:
            try:
                module = __import__(ObmLogger.PLUGINS_RELATIVE_PATH + "." + (pluginName.lower() + ".")*2, globals(), locals(), [pluginName], -1)
                result.append(getattr(module, pluginName)(self))
                logging.info("Module '" + pluginName + "' succesfully loaded.")
                # check for logging files directories
                logsDir = self.get_config_value(self.GENERAL, self.OBM_LOGS_DIR_NAME)
                logsDir = os.path.join(logsDir, pluginName)
                if (not os.path.exists(logsDir)):
                    os.mkdir(logsDir)
                logsDir = self.get_config_value(self.GENERAL, self.OBM_PROCESSED_LOGS_DIR_NAME)
                logsDir = os.path.join(logsDir, pluginName)
                if (not os.path.exists(logsDir)):
                    os.mkdir(logsDir)
            except Exception, e:
                logging.error("Error while trying to load plugin '" +
                              pluginName + "': " + str(e))
        return result

    def list_available_plugins(self):
        """Returns a list of plugins classes which are available for loading."""
        result = []
        # gets current real path
        pluginPath = inspect.getsourcefile(ObmLogger)
        pluginPath = os.path.dirname(pluginPath)
        # adds plugins relative path
        pluginPath = os.path.join(pluginPath, ObmLogger.PLUGINS_RELATIVE_PATH)

        pluginPossibleCandidates = []
        # every folder in the plugins folder is a possible plugin
        for entry in os.listdir(pluginPath):
            if os.path.isdir(os.path.join(pluginPath, entry)):
                pluginPossibleCandidates.append(entry)

        # filters those which are ObmPlugins
        parentPluginsClass = plugins.obmplugin.ObmPlugin
        for entry in pluginPossibleCandidates:
            try:
                module = __import__(ObmLogger.PLUGINS_RELATIVE_PATH + ('.' + entry) * 2, globals(), locals(), [entry], -1)
                candidateClass = module.get_plugin_class()
                if issubclass(candidateClass, parentPluginsClass):
                    result.append(candidateClass)
            except Exception, e:
                logging.error("Error while trying to load plugin class: %s", (entry,))
        return result

    def set_active_plugins_list(self, pluginsClassesList):
        """Sets the active plugin list, and load them. Returns (boolean, "Execution String").

        Using the given plugin classes list, it sets the configuration file
        entry of the active plugin list, and then tries loading them.
        Returns a tuple of a boolean representing success of the call, and a String
        of the message associated to the result of the method call.
        """

        self._loggerLock.acquire()
        logging.debug('Start setting active plugin list. OBM logger locked.')
        result = None
        if self.is_logging(False):
            result = (False, "Logging must be stopped to modify the active plugin list.")
        else:
            newList = []
            # we remove the duplicates
            pluginsClassesSet = set(pluginsClassesList)

            for pluginClass in pluginsClassesSet:
                # we keep only the plugin class name, not the canonical one
                valueToAppend = str(pluginClass).split('.')[-1]
                newList.append(valueToAppend)
            newActiveAlreadyInOldActiveCount = 0
            for entry in pluginsClassesSet:
                for activePluginObject in self._activePluginsList:
                    if isinstance(activePluginObject, entry):
                        newActiveAlreadyInOldActiveCount += 1
                        break
            if newActiveAlreadyInOldActiveCount == len(pluginsClassesSet):
                result = (True, "No change to plugin active list to be set.")
            else:
                result = (True, "Sets new active plugin list: " + str(newList))
                self.get_config().set(self.GENERAL, self.LIST_OF_ACTIVE_PLUGINS, newList)
                self.get_config().save_config()
                self.load_active_plugins()
        logging.info("Setting active plugin execution %s. %s" %
                     (result[0] and 'successful' or 'failed', result[1]))
        self._loggerLock.release()
        logging.debug('Active plugin list has been set. OBM logger lock released.')
        return result

    def log(self):
        logging.info("OpenBmap logger runs.")
        self._loggerLock.acquire()
        logging.debug('OBM logger locked by log().')
        scanSpeed = self.get_config_value(self.GENERAL, self.SCAN_SPEED_DEFAULT)
        minSpeed = self.get_config_value(self.GENERAL, self.MIN_SPEED_FOR_LOGGING)
        maxSpeed = self.get_config_value(self.GENERAL, self.MAX_SPEED_FOR_LOGGING)


        startTime = datetime.now()
        now = datetime.now();
        logging.info("Current date and time is: %s" % now)
        #("yyyy-MM-dd HH:mm:ss.000");
        #adate = now.strftime("%Y-%m-%d %H-%M-%S.")
        # "%f" returns an empty result... so we compute ms by ourself.
        #adate += str(now.microsecond/1000)[:3]
        #logging.debug("LogGenerator - adate = " + adate)
        #"yyyyMMddHHmmss"
        adate2 = now.strftime("%Y%m%d%H%M%S")
        logging.debug("LogGenerator - adate2 = " + adate2)
        #ToString("dd MM yyyy") + " at " + dt.ToString("HH") + ":" + dt.ToString("mm");
        #adate3 = now.strftime("%d %m %Y at %H:%M")
        #logging.debug("LogGenerator - adate3 = " + adate3)

        if self._gsm.call_ongoing():
            # When a call is ongoing, the signal strength diminishes
            # (without a DBus signal to notify it), and neighbour cells data returned is garbage:
            # thus we do not log during a call.
            # I fear that when the framework notifies this program about call status change, some
            # time has passed since the modem has taken it into account. This could result in effects
            # described above (e.g. the neighbour cells data we have read is already garbage), but as
            # we still have
            # not received and taken into account the call, we don't know that the data is bad. To
            # prevent this, I check just before reading the data, and I will check again just before
            # writing it, hoping to have let enough time to never see the (possible?) situation
            # described above.
            logging.info('Log canceled because a call is ongoing.')
        else:
            (validGsm, servingCell, neighbourCells) = self.get_gsm_data()
            (validGps, tstamp, lat, lng, alt, pdop, hdop, vdop, spe, heading) = self.get_gps_data()

            duration = datetime.now() - startTime
            timeLimitToGetData = 2

            if (duration.seconds > timeLimitToGetData):
                #to be sure to keep data consistent, we need to grab all the info in a reasonable amount
                # of time. At 50 km/h, you go about 15 m / second.
                # Thus you should spend only little time to grab everything you need.
                logging.warning('Log rejected because getting data took %i second(s), limit is %i second(s)'
                             % (duration.seconds, timeLimitToGetData))
            elif spe < minSpeed:
                # the test upon the speed, prevents from logging many times the same position with the same cell.
                # Nevertheless, it also prevents from logging the same position with the cell changing...
                logging.info('Log rejected because speed (%g) is under minimal speed (%g).' % (spe, minSpeed))
            elif spe > maxSpeed:
                logging.info('Log rejected because speed (%g) is over maximal speed (%g).' % (spe, maxSpeed))
            elif validGps and validGsm:
                self.write_obm_log(adate2, tstamp, servingCell, lng, lat, alt, spe, heading, hdop, vdop, pdop,
                                   neighbourCells)
            else:
                logging.info('Data were not valid for creating openBmap log.')
                logging.info("Validity=%s, MCC=%s, MNC=%s, lac=%s, cid=%s, strength=%i, act=%s, tav=%s, rxlev=%s"
                              % ((validGsm,) + servingCell) )
                logging.info("Validity=%s, lng=%f, lat=%f, alt=%f, spe=%f, hdop=%f, vdop=%f, pdop=%f" \
                              % (validGps, lng, lat, alt, spe, hdop, vdop, pdop))

            self.notify_observers()
        duration = datetime.now() - startTime
        logging.info("Logging loop ended, total duration: %i sec." % duration.seconds)

        if not self._logging:
            logging.info('Logging loop is stopping.')
            self._loggingThread = None
            self.set_current_remember_cells_structure_id()
        else:
            logging.info('Next logging loop scheduled in %d seconds.' % scanSpeed)
        # storing in 'result' prevents modification of the return value between
        # the lock release() and the return statement.
        result = self._logging
        self._loggerLock.release()
        logging.debug('OBM logger lock released by log().')
        # together with timeout_add(). self._logging is True if it must keep looping.
        return result
        
    def start_logging(self):
        """Schedules a call to the logging method, using the scanning time."""
        if not self._loggerLock.acquire(False):
            logging.debug('OBM logger is already locked. Probably already running. Returning...')
            return
        logging.debug('OBM logger locked by start_logging().')
        if self._logging:
            logging.debug('OBM logger is already running.')
        else:
            self._logging = True
            scanSpeed = self.get_config_value(self.GENERAL, self.SCAN_SPEED_DEFAULT)
            self.set_current_remember_cells_structure_id()
            self._loggingThread = gobject.timeout_add_seconds( scanSpeed, self.log )
            logging.info('start_logging: OBM logger scheduled every %i second(s).' % scanSpeed)

            for plugin in self._activePluginsList:
                plugin.init()
                # we add a pair plugin object / scheduled id
                newEntry = (plugin, gobject.timeout_add_seconds(plugin.get_logging_frequency(),
                                                                plugin.do_iteration))
                self._activePluginScheduledIds.append(newEntry)
                logging.info("Plugin " + plugin.get_id() + " scheduled every " +
                              str(plugin.get_logging_frequency()) + " second(s).")
        # be sure to notify as soon as possible the views, for better feedback

        self.notify_observers()
        self._loggerLock.release()
        logging.debug('OBM logger lock released by start_logging().')
    
    def stop_logging(self):
        """Stops the logging method to be regularly called."""
        if not self._loggerLock.acquire(False):
            logging.debug('OBM logger is already locked. Probably already running.')
            gobject.idle_add(self.stop_logging, )
            logging.info('OBM logger currently locked. Will retry stopping it later.')
        else:
            logging.debug('OBM logger locked by stop_logging().')
            self._logging = False
            logging.info('Requested logger to stop.')

            for plugin, scheduledId in self._activePluginScheduledIds:
                logging.debug('Unscheduled plugin %s', (plugin.get_id()))
                gobject.source_remove(scheduledId)
            self._activePluginScheduledIds = []

            self._loggerLock.release()
            logging.debug('OBM logger lock released by stop_logging().')
        
    def set_current_remember_cells_structure_id(self, id = None):
        """Sets a new structure to remember cells seen.

        If id is not provided, then tries: YY-mm-DD_HH:MM:SS.
        """
        if id == None:
            self._gsm.set_current_remember_cells_id(datetime.now().strftime("%Y-%m-%d_%H:%M:%S"))
        else:
            self._gsm.set_current_remember_cells_id(id)

    #===== observable interface =======
    def register(self, observer):
        """Called by observers to be later notified of changes."""
        self._observers.append(observer)
        
    def notify_observers(self):
        for obs in self._observers:
            gobject.idle_add(obs.notify, )
        
        
    def get_gsm_data(self):
        """Returns Fields validity boolean, MCC, MNC, lac, cid, signal strength, tuple of neighbour cells dictionaries.
        
        Each neighbour cell dictionary contains lac and cid fields.
        They may contain rxlev, c1, c2, and ctype.
        If MCC has changed, triggers writing of log file.
        """
        
        result = self._gsm.get_gsm_data()
        currentMcc = result[1][0]
        if currentMcc != self._mcc:
            # as soon as we have changed from MCC (thus from country), we save the logs because
            # for now the log files have the MCC in their name, to make easy to dispatch them.
            # Thus, a log file is supposed to contain only one MCC related data.
            logging.info("MCC has changed from '%s' to '%s'." % (self._mcc, currentMcc))
            self.write_obm_log_to_disk()
            self._mcc = currentMcc
        else:
            logging.debug("MCC unchanged (was '%s', is '%s')" % (self._mcc, currentMcc))
        return result

    def get_seen_cells_stats(self):
        """Returns the number of cells which have been seen.

        If the logger is on, the last remember structure contains the cells
        seen while logging, since last 'start'.

        Returns a tuple:
        number of serving cells seen in last remember structure,
        number of neighbour cells seen in last remember structure,
        number of serving cells seen since launch,
        number of neighbour cells seen since launch
        """
        return self._gsm.get_seen_cells_stats()

    def get_gps_data(self):
        """Return validity boolean, time stamp, lat, lng, alt, pdop, hdop, vdop, speed in km/h, heading."""
        (valPos, tstamp, lat, lng, alt, pdop, hdop, vdop) = self._gps.get_GPS_data()
        (valSpe, speed, heading) = self._gps.get_course()
        # knots * 1.852 = km/h
        return (valPos and valSpe, tstamp, lat, lng, alt, pdop, hdop, vdop, speed * 1.852, heading)
    
    def get_credentials(self):
        """Returns openBmap login, password."""
        return (self.get_config_value(self.CREDENTIALS, self.OBM_LOGIN),
                self.get_config_value(self.CREDENTIALS, self.OBM_PASSWORD))

    def set_credentials(self, login, password):
        """Sets the given login and password, saves the config file."""
        config.set(config.CREDENTIALS, config.OBM_LOGIN, login)
        config.set(config.CREDENTIALS, config.OBM_PASSWORD, password)
        config.save_config()
        logging.info('Credentials set to \'%s\', \'%s\'' % (login, password) )

    def is_logging(self, synchronised=True):
        """Returns True if logging plugin(s) is(are) scheduled or working. False otherwise."""
        if synchronised:
            self._loggerLock.acquire()
            logging.debug('OBM logger locked by is_logging().')
        else:
            logging.debug('OBM logger unsynchronised call to is_logging().')

        result = (self._loggingThread != None)
        logging.debug('Is the GSM logger running? %s' % (result and 'Yes' or 'No') )
        if not result:
            # GSM plugin is not running
            if len(self._activePluginScheduledIds) > 0:
                # we check if plugin(s) is(are) scheduled
                logging.debug('Plugin(s) are still scheduled.')
                result = True
            else:
                # we check every plugin is not currently working
                for plugin in self._activePluginsList:
                    result = plugin.is_working()
                    logging.debug('Is the plugin %s running? %s' % (plugin.get_id(), result and 'Yes' or 'No') )
                    if result:
                        # this plugin is working
                        break
        if synchronised:
            self._loggerLock.release()
            logging.debug('OBM logger lock released by is_logging().')
        return result
    #===== end of observable interface =======

    #===== observer interface =======
    def notify(self):
        """This method is used by observed objects to notify about changes."""
        self.notify_observers()
    #===== end of observer interface =======
            
    def simulate_gps_data(self):
        """Return simulated validity boolean, time stamp, lat, lng, alt, pdop, hdop, vdop, speed in km/h, heading."""
        return (True, 345678, 2.989123456923999, 69.989123456123444, 2.896, 6.123, 2.468, 3.1, 3.456, 10)
    
    def simulate_gsm_data(self):
        """Return simulated Fields validity boolean, (MCC, MNC, lac, cid, signal strength, act), neighbour cells."""
        return (True, ('208', '1', '123', '4', -123, 'GSM'), (
                                                              {'mcc':'123',
                                                               'mnc':'02',
                                                               'cid':'123',
                                                               'rxlev':456,
                                                               'c1':-123,
                                                               'c2':-234,
                                                               'ctype':'GSM'}))
        
#----------------------------------------------------------------------------#
# program starts here
#----------------------------------------------------------------------------#
dbus.mainloop.glib.DBusGMainLoop( set_as_default=True )

if not os.path.exists(ObmLogger.APP_HOME_DIR):
    print('Main directory does not exists, creating \'%s\'' % 
                         ObmLogger.APP_HOME_DIR)
    os.mkdir(ObmLogger.APP_HOME_DIR)
            
logging.basicConfig(filename=ObmLogger.TEMP_LOG_FILENAME,
            level=logging.INFO,
            format='%(asctime)s %(message)s', 
            filemode='w',)
config = Config(ObmLogger.CONFIGURATION_FILENAME)

if __name__ == '__main__':
    #obmlogger = ObmLogger()
    #obmlogger.init_openBmap()
    
    mainloop = gobject.MainLoop()
    try:
        # start main loop, to receive DBus signals
        mainloop.run()
    except KeyboardInterrupt:
        logging.info("Keyboard interrupted, exiting...")
        mainloop.quit()
    else:
        logging.info("normal exit.")
        sys.exit( 0 )
