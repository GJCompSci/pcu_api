### pcu_sequencer.py : A document to contain the high-level sequencer code for all 5 named positions of the PCU
### Authors : Emily Ramey, Grace Jung
### Date : 11/22/21

### TODO:
# - Add a check for negative motors (throw an error from the motor function when detected)
# - Add an initial position check so motors don't have to be retracted

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

import PCU_util as util
from positions import PCUPos
from motors import PCUMotor

# Static/global variables
TIME_DELAY = 0.5 # seconds
HOME = 0 # mm

MOVE_TIME = 45 # seconds
CLEARANCE_PMASK = 35 # mm, including mask radius
CLEARANCE_FIBER = 35 # mm, including fiber radius

# Undefined value for mini-move channels
RESET_VAL = -999.9 # mm, theoretically

### Logging
coloredlogs.DEFAULT_LOG_FORMAT = '%(asctime)s [%(levelname)s] %(message)s'
coloredlogs.DEFAULT_DATE_FORMAT = '%Y-%m-%d %H:%M:%S.%f'
coloredlogs.install(level='DEBUG')
log = logging.getLogger('')

### Set port number
port = '8609'
log.info(f'Setting server port to {port}')
os.environ['EPICS_CA_SERVER_PORT'] = port

# Config file and motor numbers
# FIX DEPLOY
config_file = "./PCU_configurations.yaml"
motor_file = "./motor_configurations.yaml"

# Load configuration files
# try:
#     # Open and read config file with info on named positions
#     with open(config_file) as f:
#         file = f.read()
#         configurations = list(yaml.load_all(file))
#         # Load both config files
#         base_configs = configurations[0]
#         fiber_configs = configurations[1]
#         mask_configs = configurations[2]
#         # FIX-YAML version on k1aoserver-new is too old.

#     # Open and read motor file with info on valid motors
#     with open(motor_file) as f: # FIX Z-STAGE
#         file = f.read()
#         motor_info = yaml.load(file)
#         valid_motors = motor_info['valid_motors']
#         tolerance = motor_info['tolerance']
#         fiber_limits = motor_info['fiber_limits']
#         mask_limits = motor_info['mask_limits']
        
# except:
#     print("Unable to read configuration files. Shutting down.")
#     sys.exit(1)

# Load configuration files
# base_configs, fiber_configs, mask_configs = util.load_configurations()
# motor_info = util.load_motors()

# # Assign motor info to variables
# valid_motors = motor_info['valid_motors']
# tolerance = motor_info['tolerance']
# fiber_limits = motor_info['fiber_limits']
# mask_limits = motor_info['mask_limits']

# # Assign config info to variables
# all_configs = dict(base_configs, **fiber_configs, **mask_configs)
# user_configs = dict(fiber_configs, **mask_configs)

class PCUStates(Enum):
    INIT = 0
    INPOS = 1
    MOVING = 2
    FAULT = 3
    TERMINATE = 4

# Class containing state machine
class PCUSequencer(Sequencer):

    # FIX Z-STAGE
    home_Z = {'m3':0, 'm4':0}
    
    # -------------------------------------------------------------------------
    # Initialize the sequencer
    # -------------------------------------------------------------------------
    def __init__(self, prefix="k1:ao:pcu", tickrate=0.5):
        super().__init__(prefix, tickrate=tickrate)
        
        # Create new channel for metastate
        self._seqmetastate = self.ioc.registerString(f'{prefix}:stst')
        self._pos = self.ioc.registerString(f'{prefix}:pos')
        self._posRb = self.ioc.registerString(f'{prefix}:posRb')
        
        self.prepare(PCUStates)
        self.destination = ''
        self.configuration = ''
        self.motor_moves = []
        # Checks whether move has completed
        self.current_move = None
        
        # Load configurations
        self.load_config_files()
        
        # Load motor objects and channels
        self.load_motors(prefix)
        
        # A timer for runtime usage
        self.move_timer = CountdownTimer()
    
    def load_config_files(self):
        """ Loads configuration files into class variables """
        # Load configuration files
        self.base_configs, self.fiber_configs, self.mask_configs = util.load_configurations()
        motor_info = util.load_motors()
        
        # Assign motor info to variables
        self.valid_motors = motor_info['valid_motors']
        self.motor_limits = motor_info['limits']
        self.tolerance = motor_info['tolerance']
        self.fiber_limits = motor_info['fiber_limits']
        self.mask_limits = motor_info['mask_limits']

        # Assign config info to variables
        self.all_configs = dict(self.base_configs, **self.fiber_configs, **self.mask_configs)
        self.user_configs = dict(self.fiber_configs, **self.mask_configs)
    
    def load_motors(self, prefix):
        """ Loads valid motors into class variable """
        # Initialize epics PVs for motors
        self.motors = {
            m_name: PCUMotor(m_name) for m_name in self.valid_motors
        }
        
        # Register individual motor channels
        for m_name in self.motors:
            for chan_type in ['Offset', 'Pos']: # relative vs. abs moves
                chan_name = f"{m_name}{chan_type}"
                # Register IOC channel for setting mini-moves
                setattr(self, "_"+chan_name, self.ioc.registerDouble(f'{prefix}:{chan_name}', 
                                                                     initial_value=RESET_VAL))
                self.add_property(chan_name, dest_read=True)
                # Register IOC channel for readback
                setattr(self, "_"+chan_name+"Rb", self.ioc.registerDouble(f'{prefix}:{chan_name}Rb'))
                self.add_property(chan_name+"Rb")
    
    def user_configs_valid(self):
        """ Checks that the user-defined configurations are valid """
        # Check user-defined positions
        for c_name, pos in self.user_configs.items():
            config = PCUPos(pos)
            if not config.is_valid():
                # What to do if it's not valid?
                self.critical(f"Configuration {c_name} is invalid. " +
                              "Please check the motor and instrument limits before reinitializing.")
                return False
        return True
    
    def get_config(self):
        """ Gets the initial configuration of the PCU """
        # Loop through configurations
        for config, values in self.all_configs.items():
            all_match = True
            
            # Loop through motors
            for m_name in self.valid_motors:
                if not self.motor_in_position(m_name, values[m_name]):
                    all_match = False
                    break
            
            # Found a match for motor positions
            if all_match:
                return config
            
            # Pinhole mask config with offset
            if (self.pmask_extended() and 
                (not self.fiber_extended()) and 
                self.element_in_hole('pmask')):
                return 'pinhole_mask'
            
            # Fiber bundle config with offset
            if (self.fiber_extended() and
                (not self.pmask_extended()) and
                self.element_in_hole('fiber')):
                return 'fiber_bundle'
            
            return None
    
    # -------------------------------------------------------------------------
    # Motor-specific functions
    # -------------------------------------------------------------------------
    def pmask_extended(self, dest_pos=None):
        """ Checks if the pinhole mask is extended """
        if 'm3' not in self.valid_motors:
            self.critical("Motor 3 is not connected. Ensure that the motor is either "+ 
                          "fully retracted or uninstalled before proceeding.")
            return False
        
        # Check current or future position
        if dest_pos is not None: return dest_pos['m3'] > 0
        else: return not self.motor_in_position('m3', 0)
    
    def fiber_extended(self, dest_pos=None):
        if 'm4' not in self.valid_motors:
            self.critical("Motor 4 is not connected. Ensure that the motor is either "+ 
                          "fully retracted or uninstalled before proceeding.")
            return False
        
        # Check current or future position
        if dest_pos is not None: return dest_pos['m4'] > 0
        else: return not self.motor_in_position('m4', 0)
    
    def pmask_center(self):
        return self.base_configs['pinhole_mask']['m1'], self.base_configs['pinhole_mask']['m2']
    
    def fiber_center(self):
        return self.base_configs['fiber_bundle']['m1'], self.base_configs['fiber_bundle']['m2']
    
    def element_in_hole(self, element, dest_pos=None):
        """ Checks whether pmask or fiber is in the K-mirror rotator hole """
        # Get center and radius of configuration
        if element=='pmask':
            xc, yc = self.pmask_center()
            radius = CLEARANCE_PMASK
        elif element=='fiber':
            xc, yc = self.fiber_center()
            radius = CLEARANCE_FIBER
        
        # Check current or future position
        if dest_pos is None: # Current positions
            x_pos, y_pos = self.motors['m1'].get_pos(), self.motors['m2'].get_pos()
        else: # Future positions
            x_pos, y_pos = dest_pos['m1'], dest_pos['m2']
        
        return (xc - x_pos)**2 + (yc - y_pos)**2 < radius
        
    
    # -------------------------------------------------------------------------
    # Mini-move functions
    # -------------------------------------------------------------------------
    
    def add_property(self, p_name, dest_read=False):
        """ 
        Add a mini-move property to the PCUSequencer class
        named <p_name>, with or without a destructive read
        """
        chan_name = "_"+p_name
        
        # Define the getter channel for the property
        def getter(self):
            # Get the current value
            value = getattr(self, chan_name).get()
            
            if dest_read: # Clear value
                if value not in [RESET_VAL, None]:
                    getattr(self, chan_name).set(RESET_VAL)
                if value == RESET_VAL:
                    return None
            # Return set value
            return value
        
        # Set the property attributes
        setattr(PCUSequencer, p_name, # Set the self.<p_name> variable to the property described
                property( # property decorator
                    getter, # Getter method
                    lambda self, val: getattr(self, chan_name).set(val) # Setter method
                )
               )
    
    def get_mini_moves(self):
        """ Returns a dictionary of mini-moves to be taken """
        mini_moves = {}
        
        # Check all motor input channels
        for m_name in self.motors:
            offset_channel = m_name+"Offset"
            offset_request = getattr(self, offset_channel)
            # Check for requested moves
            if offset_request is not None:
                # Add to existing configuration
                if self.configuration in self.all_configs:
                    offset_request += self.all_configs[self.configuration][m_name]
                # TODO: else add it to the current position
                mini_moves[m_name] = offset_request
        
        return mini_moves
    
    def check_mini_moves(self, mini_moves):
        """ Checks that a move is valid within a configuration """
        # Check that it is the right configuration
        if self.configuration not in ['pinhole_mask', 'fiber_bundle']:
            return False
        if not ('m1' in self.valid_motors and 'm2' in self.valid_motors):
            self.critical("X and Y motors must be enabled for offsets to take place.")
            return False
        
        # Get current motor positions
        dest_pos = self.get_positions()
        # Update to new positions
        for m_name, m_dest in mini_moves.items():
            dest_pos[m_name] = m_dest
        
        if not self.check_motor_limits(dest_pos):
            return False
        
        # Get centers of XY coordinates
        xc = self.all_configs[self.configuration]['m1']
        yc = self.all_configs[self.configuration]['m2']
        x_dest = dest_pos['m1']
        y_dest = dest_pos['m2']
        
        # Check for pinhole mask moves
        if self.configuration == "pinhole_mask":
            r_circ = CLEARANCE_PMASK # mm ### what do we want the clearance to be?
            # I'm going to need the exact center of the circle we want for this
            # The values we're using now are just estimates
            
            # OK to move pinhole mask, not fiber bundle
            # Maybe this should raise an error? Can you attach a string to a 
            #     low-level error and print out a warning higher up?
            if m_name == 'm3': return True
            if m_name == 'm4': return False
            
            # Check if XY motors are outside circle bounds
            return (x_dest-xc)**2 + (y_dest-yc)**2 < r_circ**2
        
        elif self.configuration == "fiber_bundle": # Check for fiber bundle moves
            r_circ = CLEARANCE_FIBER # mm
            # OK to move fiber bundle, not pinhole mask
            if m_name == 'm3': return False
            if m_name == 'm4': return True
            
            # Check if XY motors are outside circle bounds
            self.message(f"{xc}, {x_dest}, {yc}, {y_dest}, {r_circ}")
            return (xc-x_dest)**2 + (yc-y_dest)**2 < r_circ**2
            
        else: # This shouldn't happen
            self.critical("Reached impossible state in checking mini-moves.")
            self.to_FAULT()
    
    # -------------------------------------------------------------------------
    # Regular motor-moving functions
    # -------------------------------------------------------------------------
    
    def enable_all(self):
        """ Enables all motors in the PCU """
        for m_name, motor in self.motors.items():
            motor.enable()
    
    def disable_all(self):
        for m_name, motor in self.motors.items():
            motor.disable()
    
    def get_positions(self):
        """ Returns positions of all valid motors """
        all_positions = {}
        for m_name, motor in self.motors.items():
            all_positions[m_name] = motor.get_pos()
        
        return all_positions
    
    def load_config(self, destination):
        """ Loads destination's moves into queue, clears current configuration """
        # Get ordered moves from destination state
        motor_posvals = self.all_configs[destination]
        
        # Append info to move list
        self.motor_moves.clear()
        
        # Pull back Z stages if it's a major move
        if self.configuration != destination:
            self.motor_moves.append(PCUSequencer.home_Z)
        # Note: this should make it so moves within a configuration 
        #       don't pull the Z stages back all the way

        for m_name in motor_posvals.keys():
            # Skip bad entries in the yaml file.
            if m_name not in self.valid_motors:
                continue

            # Get destination of each motor
            dest = motor_posvals[m_name]

            # Append to motor moves
            self.motor_moves.append({m_name:dest})
        
        # Clear configuration and set destination
        self.configuration = ''
        self.destination = destination

        return
    
    def trigger_move(self, m_dict):
        """ Triggers move and sets a timer to check if complete """
        for m_name, m_dest in m_dict.items():
            if m_name in self.valid_motors:
                # Get PV object for motor
                motor = self.motors[m_name]
                
                # Check that the motor is enabled
                if not motor.isEnabled():
                    self.critical(f"Motor {m_name} is not enabled.")
                    self.stop_motors()
                    self.to_FAULT()
                
                # Set position of motor
                motor.set_pos(m_dest)
        
        # Save current move to class variables
        self.current_move = m_dict
        
        # Start a timer for the move
        self.move_timer.start(seconds=MOVE_TIME)

        return
    
    def check_motor_limits(self, dest_pos):
        """ Get motor destinations and check limits """
        
        for m_name, m_lim in self.motor_limits.items():
            if m_name not in dest_pos: continue
            m_dest = dest_pos[m_name]
            if m_dest < m_lim[0] or m_dest > m_lim[1]:
                self.message(f"Limit detected for {m_name}")
                return False
        
        return True
    
    def motor_in_position(self, m_name, m_dest):
        """ Checks whether a motor (m_name) is in position (m_dest) """
        # Check for valid motor
        if m_name not in self.valid_motors:
            return False
        
        # Get PV getter for motor
        motor = self.motors[m_name]
        # Get current position
        cur_pos = motor.get_pos()
        
        # Compare to destination within tolerance, return False if not reached
        t = self.tolerance[m_name]
        # Lower and upper limits
        in_pos = cur_pos > m_dest-t and cur_pos < m_dest+t
        # Return whether the given motor is in position
        return in_pos
    
    def move_complete(self):
        """ Returns True when the move in self.current_move is complete """
        # Get current motor motions
        m_dict = self.current_move
        # Return True if no moves are taking place
        if m_dict is None: return True
        
        # Get current positions and compare to destinations
        for m_name, m_dest in m_dict.items():
            if m_name in self.valid_motors:
                if not self.motor_in_position(m_name, m_dest):
                    return False
                
        # Return True if motors are in position and release current_move
        self.message(f"Move {self.current_move} complete!")
        self.current_move = None
        return True
    
    def stop_motors(self):
        """ Stops motors only """
        
        # Message the thread
        self.critical("Stopping all motors.")
        
        # Clear future moves from queue
        self.current_move = None
        self.motor_moves.clear()
        # Set config to unknown
        self.configuration = ''
        self.destination = ''
        
        # Stop motors
        for _, pv in self.motors.items():
            pv.stop()
    
    def stop(self):
        """ Stops all PCU motors and halts operation """
        # Stop motors
        self.stop_motors()
        
        # Call the superclass stop method
        super().stop()
    
    def home_motors(self):
        """ Homes the motors (z-stages first, then X and Y) """
        # Need to check if it will wait for the motor to home
        for motor in reversed(self.motors):
            pass # Ask Sylvain when he's less busy
    
    # -------------------------------------------------------------------------
    # I/O processing
    # -------------------------------------------------------------------------
    
    def process_request(self):
        """ Processes input from the request keyword """
        # Check the request keyword
        request = self.seqrequest.lower()

        if request == '':
            return

        if request == 'shutdown':
            if self.state != PCUStates.MOVING:
                self.message("Shutting down sequencer.")
                super().stop()
            else:
                self.critical("Aborting sequencer.")
                self.stop()
        
        if request == 'enable':
            if self.state == PCUStates.INPOS:
                self.enable_all()
            else:
                self.critical("PCU must be in INPOS state to enable motors.")
        
        if request == 'disable':
            if self.state == PCUStates.INPOS:
                self.disable_all()
            elif self.state == PCUStates.MOVING:
                self.stop_motors()
                self.disable_all()
            else:
                self.critical(f"Invalid input for state {self.state.name}")
        
        # Stop the PCU and go to USER_DEF position
        if request == 'stop':
            if self.state==PCUStates.MOVING:
                self.stop_motors()
                self.to_INPOS()
            else:
                self.critical("PCU is not moving.")
        
        if request == 'reinit':
            if self.state != PCUStates.MOVING:
                self.to_INIT()
            else:
                self.critical("Send stop signal before reinitializing.")
    
    def process_pos_request(self):
        """ Processes a request for a configuration change """
        request = self.config_request.lower()

        if request == '':
            return
        
        ### Request from a position
        if self.state == PCUStates.INPOS:
            destination = request
            if destination in self.all_configs:
                self.message(f"Loading {destination} state.")
                # Load next configuration (sets self.destination)
                self.load_config(destination)
                # Start move
                self.to_MOVING()
            else: self.critical(f'Invalid configuration: {destination}')
            ### Request from MOVING
        elif self.state == PCUStates.MOVING:
            self.critical("Send stop signal before moving to new position.")
            ### Request from FAULT
        elif self.state == PCUStates.FAULT:
            self.critical("Reinitialize the PCU sequencer before moving.")
    
    def checkabort(self):
        """Check if the abort flag is set, and drop into the FAULT state"""
        if self.seqabort:
            self.critical('Aborting sequencer!')
            self.stop()

        return False
    
    def checkmeta(self):
        """ Checks metastate and position of PCU """
        # Get metastate
        self.metastate = self.state.name
        # Make sure configuration is none unless in position
        if self.state != PCUStates.INPOS:
            self.configuration = ''
        if self.state==PCUStates.INPOS and self.configuration=='':
            self.configuration = 'user_def'
    
    def check_offsets(self):
        """ Checks offsets from the current configuration """
        if self.configuration not in self.all_configs: # No metastate
            for m_name in self.motors:
                setattr(self, m_name+"OffsetRb", 0)
        else: # Get offset positions
            base_positions = self.all_configs[self.configuration]
            motor_positions = self.get_positions()
            for m_name in motor_positions:
                offset = motor_positions[m_name] - base_positions[m_name]
                setattr(self, m_name+"OffsetRb", offset)
    
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
    
    @property
    def config_request(self):
        request = self._pos.get()
        if request not in [None, '']:
            self._pos.set(''.encode('UTF-8'))
        return request
    
    @property
    def configuration(self):
        cur_pos = self._posRb.get()
        return cur_pos
    @configuration.setter
    def configuration(self, val):
        if val==None: val = ''
        self._posRb.set(val.encode('UTF-8'))
    
    # -------------------------------------------------------------------------
    # Init state
    # -------------------------------------------------------------------------
    
    def process_INIT(self):
        ###################################
        ## Any initialization stuff here ##
        ###################################
        
        try:
            # Load and check config files
            self.load_config_files()
            if not self.user_configs_valid():
                self.to_FAULT()
                return
            # Will return configuration or None
            self.configuration = self.get_config()
            
            self.to_INPOS()
        
        # Enter the fault state if a channel is disconnected while running
        except PVDisconnectException as err:
            self.critical(str(err))
            self.stop_motors()
            self.to_FAULT()
        
        # Home / initialize stages
        # Re-read the config files
        # Make XYZ moves possible
        ###################################
    
    # -------------------------------------------------------------------------
    # INPOS state
    # -------------------------------------------------------------------------
    
    def process_INPOS(self):
        """ Processes the INPOS state """
        ######### Add mini-moves here ##########
        self.checkabort()
        self.checkmeta()
        self.check_offsets()
        
        try:
            # Check for mini-moves (dithers)
            mini_moves = self.get_mini_moves()
            # Found mini-moves
            if len(mini_moves) != 0:
                # Trigger a PCU move
                if self.check_mini_moves(mini_moves):
                    # Load mini-moves into queue
                    self.motor_moves.append(mini_moves)
                    # Set destination to preserve configuration
                    self.destination = self.configuration
                    # Go to moving
                    self.to_MOVING()
                else: # Warn user
                    self.critical(f"Invalid move for configuration {self.configuration}: {mini_moves}")
            
            self.process_request()
            self.process_pos_request()

        # Enter the faulted state if a channel is disconnected while running
        except PVDisconnectException as err:
            self.critical(str(err))
            self.stop_motors()
            self.to_FAULT()
    
    # -------------------------------------------------------------------------
    # MOVING state
    # -------------------------------------------------------------------------
    
    def process_MOVING(self):
        """ Process the MOVING state """
        self.checkabort()
        self.checkmeta()
        self.check_offsets()
        
        try:
            
            # Check for mini-move keywords
            mini_moves = self.get_mini_moves()
            if len(mini_moves) != 0:
                self.critical("Send stop signal before moving to new position.")

            # Check the request keyword and
            # start the reconfig process, if necessary
            self.process_request()
            self.process_pos_request()
            
            # If there are moves in the queue and previous moves are done
            if len(self.motor_moves) != 0 and self.move_complete():
                # There are moves in the queue, pop next move from the list and trigger it
                next_move = self.motor_moves.pop(0)
                self.message(f"Triggering move, {next_move}.")
                self.trigger_move(next_move)
            elif len(self.motor_moves) == 0 and self.move_complete():
                # No moves left to make, finish and change state
                self.message("Finished moving.")
                # Change configuration and destination keywords
                self.configuration = self.destination
                self.destination = ''
                # Move to in-position state
                self.to_INPOS()
            else: # Move is in progress
                pass
            
            # Check if move has timed out
            if self.move_timer.expired:
                self.critical("Move failed due to motor timeout.")
                self.stop_motors()
                self.to_FAULT()

        # Enter the faulted state if a channel is disconnected while running
        except PVDisconnectException as err:
            self.critical(str(err))
            self.stop_motors()
            self.to_FAULT()
    
    # -------------------------------------------------------------------------
    # FAULT state
    # -------------------------------------------------------------------------
    
    def process_FAULT(self):
        """ Processes the FAULT state """
        self.checkabort()
        self.checkmeta()
        
        # Respond to request channel
        self.process_request()
        self.process_pos_request()
        
    
    # -------------------------------------------------------------------------
    # TERMINATE state
    # -------------------------------------------------------------------------
    def process_TERMINATE(self):
        pass

# -------------------------------------------------------------------------
# Main function
# -------------------------------------------------------------------------
if __name__ == "__main__":
    
    # Setup environment variables to find the right EPICS channel
    os.environ['EPICS_CA_ADDR_LIST'] = 'localhost:8600 localhost:8601 localhost:8602 ' + \
        'localhost:8603 localhost:8604 localhost:8605 localhost:8606 localhost:5064'
    os.environ['EPICS_CA_AUTO_ADDR_LIST'] = 'NO'

    # Define an enum of task names
    class TASKS(Enum):
        SequencerTask1 = 0

    # The main sequencer
    setup = PCUSequencer(prefix='k1:ao:pcu')

    # Create a task pool and register the sequencers that need to run
    tasks = Tasks(TASKS, 'k1:ao:pcu', workers=len(TASKS))
    tasks.register(setup, TASKS.SequencerTask1)

    # Start everything
    log.info('Starting sequencer.')
    tasks.run()
