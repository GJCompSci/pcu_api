### pcu_sequencer.py : A document to contain the high-level sequencer code for all 5 named positions of the PCU
### Authors : Emily Ramey, Grace Jung
### Date : 11/22/21

### Imports
from transitions import Machine, State
import yaml
import numpy as np
import time
# from epics import PV

TIME_DELAY = 0.5 # seconds
HOME = 0 # mm

# Config file and motor numbers
yaml_file = "PCU_configurations.yaml"
valid_motors = [f"m{i}" for i in np.arange(1,5)]

# Open and read YAML file
with open(yaml_file) as file:
    state_lookup = yaml.load(file, Loader=yaml.FullLoader)

# Getter and Setter channel names for each motor
set_pattern = "k1:ao:pcu:ln:{}:posval"
get_pattern = "k1:ao:pcu:ln:{}:posvalRb"

def move_motor(m_name, m_dest, block=True):
    print(f"Setting {m_name} to {m_dest} mm") # temporary
    return
    
    # Get PVs for motor
    m_get = RunPCU.motors['get'][m_name]
    m_set = RunPCU.motors['set'][m_name]
    
    # Send move command
    m_set.put(m_dest)
    
    if block:
        # Block until motor is moved
        cur_pos = m_get.get()
        print(f"Getting {m_name} position")
        while cur_pos != m_dest: # Need a timeout and a tolerance or it may run forever
            # Get new position
            cur_pos = m_get.get()
            # Wait for a short time
            time.sleep(TIME_DELAY)

# Class containing state machine
class RunPCU:
    
    # Initialize PCU states, on_enter moves motors into position
    states = []
    for name in state_lookup:
        states.append(State(name=name, on_enter='move_motors'))
    
    # Initialize epics PVs for motors
    motors = {
        'get': {},
        'set': {}
    }
    # One getter and one setter PV per motor
    for m_name in valid_motors:
        motors['get'][m_name] = get_pattern.format(m_name)
        motors['set'][m_name] = set_pattern.format(m_name)
    
    print(motors)
    
    # Initialize RunPCU instance
    def __init__(self):
        # Models 5 telescopes states, home Z stages before changing states
        self.machine = Machine(model = self, states = RunPCU.states,
                               before_state_change='home_Zstages',
                               initial = 'telescope')
    
    # Homes Z-stages before changing configuration
    def home_Zstages(self):
        move_motor('m3', HOME, block=False)
        move_motor('m4', HOME, block=True)
    
    # Moves stages/motors to new configuration
    def move_motors(self):
        # Get position values for new state
        motor_posvals = state_lookup[self.state]
        print(f"Moving motor to {motor_posvals} in state {self.state}")
        
        # Move each motor, in order
        for m_name in valid_motors:
            # Get desired motor position in current state
            m_dest = motor_posvals[m_name]
            # Move motor
            move_motor(m_name, m_dest)
            
            