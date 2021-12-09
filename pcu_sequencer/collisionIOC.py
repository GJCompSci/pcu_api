### collisionIOC.py : A document to monitor the individual PCU motor channels and stop them if there is an imminent collision
### Authors : Emily Ramey
### Date : 12/8/21

### Imports
from transitions import Machine, State
from kPySequencer.Sequencer import Sequencer, PVDisconnectException, PVConnectException
from kPySequencer.Tasks import Tasks
from kPySequencer.CountdownTimer import CountdownTimer
import logging, coloredlogs
import yaml
import numpy as np
import time
from epics import PV
from enum import Enum
import signal
import sys
import os
import operator

import PCU_util as util
from positions import PCUPos, PCUMove
from motors import PCUMotor

### Logging
coloredlogs.DEFAULT_LOG_FORMAT = '%(asctime)s [%(levelname)s] %(message)s'
coloredlogs.DEFAULT_DATE_FORMAT = '%Y-%m-%d %H:%M:%S.%f'
coloredlogs.install(level='DEBUG')
log = logging.getLogger('')

### Set port number
port = '5066'
log.info(f'Setting server port to {port}')
os.environ['EPICS_CA_SERVER_PORT'] = port

KMIRR_RADIUS = 50 # mm - true radius of the k-mirror rotator

class collisionStates(Enum):
    INIT = 0
    MONITORING = 1
    STOPPED = 2
    RESTRICTED = 3
    FAULT = 4
    TERMINATE = 5

# Class containing state machine
class collisionSequencer(Sequencer):
    
    # -------------------------------------------------------------------------
    # Initialize the sequencer
    # -------------------------------------------------------------------------
    def __init__(self, prefix="k1:ao:pcu:collisions", tickrate=0.5):
        super().__init__(prefix, tickrate=tickrate)
        
        self.allowed_motors = {}
        self.load_config_files()
        self.load_motors(prefix)
        
        # Create new channel for metastate
        self._seqmetastate = self.ioc.registerString(f'{prefix}:stst')
        self.same_message = False
        
        self.prepare(collisionStates)
    
    def load_config_files(self): # Figure this out later
        """ Loads configuration files into class variables """
        # Update position class
        PCUPos.load_motors()
        
        # Load configuration files
        all_configs = util.load_configurations()
        self.base_configs = PCUPos.from_dict(all_configs[0])
        self.fiber_configs = PCUPos.from_dict(all_configs[1])
        self.mask_configs = PCUPos.from_dict(all_configs[2])
        
        # Load motor configuration info
        motor_info = util.load_motors()
        
        # Assign motor info to variables
        self.valid_motors = motor_info['valid_motors']
        self.motor_limits = motor_info['motor_limits']
        self.tolerance = motor_info['tolerance']

        # Assign config info to variables
        self.all_configs = dict(self.base_configs, **self.fiber_configs, **self.mask_configs)
        self.user_configs = dict(self.fiber_configs, **self.mask_configs)
    
    def load_motors(self, prefix):
        """ Loads valid motors into class variable """
        # Initialize epics PVs for motors
        self.motors = {
            m_name: PCUMotor(m_name) for m_name in self.valid_motors
        }
    
    def stop(self):
        """ halts operation """
        self.stop_motors()
        # Call the superclass stop method
        super().stop()
    
    def current_pos(self):
        """ Returns positions of all valid motors """
        cur_pos = PCUPos()
        for m_name, motor in self.motors.items():
            cur_pos[m_name] = motor.get_pos()
        
        return cur_pos
    
    def commanded_pos(self):
        """ Return commanded position of all valid motors """
        pos = PCUPos()
        for m_name, motor in self.motors.items():
            pos[m_name] = motor.get_commanded()
            
        return pos
    
    def stop_motors(self):
        """ Stops motors only """
        
        # Message the thread
        self.critical("Stopping all motors.")
        
        # Stop motors
        for _, pv in self.motors.items():
            pv.stop()
        
        self.reset_command()
        
        for _, pv in self.motors.items():
            pv.disable()
    
    def motors_enabled(self):
        enabled = []
        for _, motor in self.motors.items():
            enabled.append(motor.isEnabled())
        return enabled
    
    def load_restricted_moves(self):
        """ Loads moves possible from an invalid state """
        self.allowed_motors.clear()
        
        # Check current position
        cur_pos = self.current_pos()
        fiber_in_hole = cur_pos.in_hole('fiber', check_rad=KMIRR_RADIUS) # Real bounds
        fiber_allowed = cur_pos.in_hole('fiber') # Allowed bounds
        mask_in_hole = cur_pos.in_hole('mask', check_rad=KMIRR_RADIUS) # Real bounds
        mask_allowed = cur_pos.in_hole('mask') # Allowed bounds
        
        ### Calculate which axis/axes should allow motion
        move_to_center = False
        # Fiber positioning
        if not fiber_in_hole and cur_pos.fiber_extended():
            self.critical("The fiber bundle is extended. Please retract the fiber bundle stage (motor 4).")
            self.allowed_motors['m4'] = operator.le
        elif fiber_in_hole and not fiber_allowed: # Outside standard bounds
            self.critical(f"The fiber bundle is outside of allowed bounds. " \
                          f"Please move towards the k-mirror center, {PCUPos.fiber_center}.")
            # Get relevant signs
            instr_center = PCUPos(m1=PCUPos.fiber_center[0], m2=PCUPos.fiber_center[1], m3=0)
            move_to_center = True
        
        # Pinhole mask positioning
        if not mask_in_hole and cur_pos.mask_extended():
            self.critical("The pinhole mask is extended. Please retract the pinhole mask (motor 3).")
            self.allowed_motors['m3'] = operator.le
        elif mask_in_hole and not mask_allowed:
            self.critical("The pinhole mask is outside of allowed bounds." \
                         f"Please move towards the k-mirror center, {PCUPos.mask_center}.")
            # Get relevant signs
            instr_center = PCUPos(m1=PCUPos.mask_center[0], m2=PCUPos.mask_center[1], m3=0)
            move_to_center = True
        
        if cur_pos.fiber_extended and cur_pos.mask_extended and move_to_center:
            # Must be fixed manually
            self.critical("The PCU stages must be reset manually.")
            self.to_STOPPED()
        elif move_to_center:
            pos_diff = instr_center - cur_pos
            for m_name in ['m1', 'm2']: # Append operators to valid motors
                if pos_diff[m_name] > 0: self.allowed_motors[m_name] = operator.ge
                if pos_dif[m_name] < 0: self.allowed_motors[m_name] = operator.le
    
    def reset_command(self):
        """ Resets the commanded positions of the motors """
        cur_pos = self.current_pos()
        for m_name in self.valid_motors:
            motor = self.motors[m_name]
            motor.set_pos(cur_pos[m_name])
    
    def check_all_pos(self):
        """ Checks all positions for validity """
        cur_pos = self.current_pos()
        future_pos = self.commanded_pos()

        if not cur_pos.is_valid():
            self.critical(f"Current position is invalid: {cur_pos}. Disabling all motors.")
            self.stop_motors()
            return False
        elif not future_pos.is_valid():
            self.critical(f"Commanded position is invalid: {future_pos}. Disabling all motors.")
            self.stop_motors()
            return False
        
        return True
    
    def check_future_pos(self):
        """ Checks for restricted moves """
        # Get position difference
        cur_pos = self.current_pos()
        future_pos = self.future_pos()
        
        # Check motor moves are in right direction
        for m_name, op in self.allowed_motors.items():
            if not op(future_pos[m_name], cur_pos[m_name]):
                self.critical("Invalid move requested.")
                self.stop_motors()
                self.to_STOPPED()
                return
    
    # -------------------------------------------------------------------------
    # I/O processing
    # -------------------------------------------------------------------------
    
    def process_request(self):
        """ Processes input from the request keyword """
        request = self.seqrequest.lower()
        
        if request=='reinit':
            if self.state==collisionStates.MONITORING or self.state==collisionStates.FAULT:
                self.to_INIT()
            elif self.current_pos().is_valid():
                self.to_INIT()
            else:
                self.critical("Cannot reinitialize from an invalid position.")
        
        if request=='allow_moves':
            if self.state==collisionStates.STOPPED:
                self.message("Turning on directional moves for safe axes.")
                self.to_RESTRICTED()
            elif self.state==collisionStates.RESTRICTED:
                self.critical("Directional moves are already enabled.")
            elif self.state==collisionStates.FAULT:
                self.critical("Sequencer must be reinitialized in order to move.")
            else:
                self.critical("All moves are enabled.")
    
    def checkabort(self):
        """ Check if the abort flag is set, and stop the sequencer if so """
        if self.seqabort:
            self.critical('Aborting sequencer!')
            self.stop_motors()
            self.stop()

        return False
    
    def checkmeta(self):
        """ Checks the metastate """
        self.metastate = self.state.name
    
    # -------------------------------------------------------------------------
    # Init state
    # -------------------------------------------------------------------------
    
    def process_INIT(self):
        self.checkmeta()
        try:
            #########################################
            ## Any other initialization stuff here ##
            #########################################

            #########################################
            PCUPos.load_motors()
            self.load_config_files()
            if self.check_all_pos():
                self.to_MONITORING()
            else: self.to_STOPPED()
            
        except PVDisconnectException as err:
            self.critical(str(err))
            self.stop_motors()
            self.to_FAULT()
    
    # -------------------------------------------------------------------------
    # MONITORING state
    # -------------------------------------------------------------------------
    def process_MONITORING(self):
        """ Check current position and stop if it's not valid """
        self.checkabort()
        self.checkmeta()
        self.process_request()
        try: # Check current and future positions
            if not self.check_all_pos():
                self.to_STOPPED()
            
            # Process user requests
            self.process_request()
        
        # Enter the FAULT state if a channel is disconnected while running
        except PVDisconnectException as err:
            self.critical(str(err))
            self.stop_motors()
            self.to_FAULT()
    
    # -------------------------------------------------------------------------
    # STOPPED state
    # -------------------------------------------------------------------------
    def process_STOPPED(self):
        # Send stop command until it's valid again?
        self.checkabort()
        self.checkmeta()
        try:
            # Make sure the motors are disabled
            if any(self.motors_enabled()):
                self.critical("Motors cannot be enabled in STOPPED state.")
                self.stop_motors()

            cur_pos = self.current_pos()

            if not self.same_message:
                if cur_pos.is_valid():
                    self.message("Current position is valid. Please reinitialize to use motors normally." \
                                "(run 'caput k1:ao:pcu:collisions:request reinit'.)")
                else:
                    self.critical("Current position is invalid. Please run 'caput k1:ao:pcu:collisions:request allow_moves'" \
                                 " to turn on directional moves only.")

            self.same_message = True

            # Process user requests
            self.process_request()

            # Reset the message counter if a new state is reached
            if self.state != collisionStates.STOPPED: self.same_message = False
        
        except PVDisconnectException as err:
            self.critical(str(err))
            self.stop_motors()
            self.to_FAULT()
    
    # -------------------------------------------------------------------------
    # RESTRICTED state
    # -------------------------------------------------------------------------
    def process_RESTRICTED(self):
        # Send stop command until it's valid again?
        self.checkabort()
        self.checkmeta()
        self.process_request()
        
        try:
            # Make sure moves are towards valid position
            self.load_restricted_moves()
            # Disable motors that aren't allowed
            for m_name in valid_motors:
                if m_name not in self.allowed_motors:
                    self.motors[m_name].disable()
            
            self.check_future_pos()
            
            # Check whether position is valid again
            cur_pos = self.current_pos()
            if not self.same_message:
                if cur_pos.is_valid():
                    self.message("Current position is valid. Please reinitialize to use motors normally.\n" \
                            "(run 'caput k1:ao:pcu:collisions:request reinit'.)")
            self.same_message = True
            
            # Process user input
            self.process_request()
            
            # Check for state change
            if self.state != collisionStates.RESTRICTED: self.same_message = False
            
        # Enter the FAULT state if a channel is disconnected while running
        except PVDisconnectException as err:
            self.critical(str(err))
            self.stop_motors()
            self.to_FAULT()
    
    # -------------------------------------------------------------------------
    # FAULT state
    # -------------------------------------------------------------------------
    def process_FAULT(self):
        self.checkabort()
        self.checkmeta()
        if not self.same_message:
            self.critical("The collision avoidance sequencer is down. Do not run the motors.")
        self.same_message = True
        
        self.process_request()
        if self.state != collisionStates.FAULT: self.same_message = False
        
    
    # -------------------------------------------------------------------------
    # TERMINATE state
    # -------------------------------------------------------------------------
    def process_TERMINATE(self):
        pass
    
    # -------------------------------------------------------------------------
    # Control channel properties (static)
    # -------------------------------------------------------------------------
    @property
    def metastate(self):
        request = self._seqmetastate.get()
        # Don't want a destructive read
        return request
    @metastate.setter
    def metastate(self, val): self._seqmetastate.set(val.encode('UTF-8'))

# -------------------------------------------------------------------------
# Main function
# -------------------------------------------------------------------------
if __name__ == "__main__":
    
    # Setup environment variables to find the right EPICS channel
#     os.environ['EPICS_CA_ADDR_LIST'] = 'localhost:8600 localhost:8601 localhost:8602 ' + \
#         'localhost:8603 localhost:8604 localhost:8605 localhost:8606 localhost:5064'
#     os.environ['EPICS_CA_AUTO_ADDR_LIST'] = 'NO'

    # Define an enum of task names
    class TASKS(Enum):
        SequencerTask1 = 0

    # The main sequencer
    setup = collisionSequencer(prefix='k1:ao:pcu:collisions')

    # Create a task pool and register the sequencers that need to run
    tasks = Tasks(TASKS, 'k1:ao:pcu:collisions', workers=len(TASKS))
    tasks.register(setup, TASKS.SequencerTask1)

    # Start everything
    log.info('Starting collision sequencer.')
    tasks.run()