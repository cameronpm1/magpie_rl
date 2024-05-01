import os
import sys
import time
import hydra
import numpy as np
import pyvista as pv
import gymnasium
from gymnasium import spaces
from astropy import units as u
from gymnasium.utils import seeding
from collections import OrderedDict
from omegaconf import DictConfig, OmegaConf
from typing import Any, Dict, Type, Optional, Union


from logger import getlogger
from space_sim.sim import Sim
#from train.make_env import make_env
from sim_prompters.one_v_one_prompter import oneVOnePrompter

logger = getlogger(__name__)

class satGymEnv(gymnasium.Env):

    def __init__(
            self,
            sim: Type[Sim],
            step_duration: float,
            max_episode_length: int,
            max_ctrl: list[float],
            action_scaling_type: str = 'clip',
            randomize_initial_state: bool = False,
            scenario_prompter: Optional[Type[oneVOnePrompter]] = None,
    ):
        """
        Args:
            sim: Space simulation
            step_duration: Time step length of Sim
            max_episode_length: Max timesteps before Sim terminates
            max_ctrl: Upper bounds on control for sat_dynamics
            normalization_method: 'clip', 'std' or 'scale'
            randomize_initial_state: If True, environment uses
                scenario_prompter to generate random scenarios
            scenario_prompter: initializes random space Sim scenarios
        """
        logger.info("Initializing env ...")

        self._episode = 0
        self._seed = None
        self.action_dim = len(max_ctrl)
        self.step_duration = step_duration
        self.max_episode_length = max_episode_length

        #Initialize Simulation
        self.sim = sim

        # Control clipping
        self.max_ctrl = max_ctrl
        self.action_scaling_type = action_scaling_type
        self.scaling_function = getattr(self,'_'+self.action_scaling_type)

        #randomize initial state or not
        self.scenario_prompter = scenario_prompter
        self.randomize_initial_state = randomize_initial_state


    @property
    def action_space(
            self,
    ) -> gymnasium.Space:
        return spaces.Box(low=-1, high=1, shape=(self.action_dim,), dtype=np.float32)

    @property
    def observation_space(
            self,
    ) -> gymnasium.Space:
        
        obs = self._get_obs()
        space = {}
        for key, val in obs.items():
            space[key] = spaces.Box(low=-np.inf, high=np.inf, shape=val.shape)

        return spaces.Dict(space)

    '''
    TO DO:
        -seed every reset?
    '''
    def reset(self, **kwargs):
        self._episode += 1
        self._step = 0
        self.sim.reset()
        return self._get_obs(), {'episode': self._episode}
    
    def step(self, action):
        sat_action = self.sim.compute_evade_control()
        self.sim.set_sat_control(sat_action)
        self.sim.set_adversary_control([self.scaling_function(action)])
        self.sim.step()
        self._step += 1
        obs = self._get_obs()
        rew = self._reward()
        terminated, truncated = self._end_episode() #end by collision, end by max episode
        
        return obs, rew, terminated, truncated, {'done': (terminated, truncated), 'reward': rew}
    
    def close(self):
        del self.sim
        
    def seed(self, seed=None):
        # Save the seed so we can re-seed during un-pickling
        self._seed = seed

        # Hash the seed to avoid any correlations
        #seed = seeding.hash_seed(seed)

        # Seed environment components with randomness
        seeds = [seed]
        seeds.extend(self.sim.seed(seed))
        #if self._state_sampler is not None:
        #    seeds.extend(self._state_sampler.seed(seed))

        return seeds
    
    def _get_obs(self) -> OrderedDict:
        """Return observation

        For attributes which can be noisy ex. hand_joint_position,
        contact_position, contact_normal etc,. both the "accurate" and
        noisy versions are inluded in the observation dictionary. The
        noisy version is the one with suffix "_noise". Helpful towards
        using assymmetric actor-critic architectures.

        """

        obs = OrderedDict()

        # Satellite
        obs['sat_state'] = self.sim.main_object.get_state().copy()

        obs['goal_state'] = np.array(self.sim.get_sat_goal().copy())

        a = 0
        o = 0
        for obstacle in self.sim.obstacles:
            #Adversaries
            if 'adversary' in obstacle.get_name():
                obs['adversary'+str(a)+'_state'] = obstacle.get_state().copy()
                a += 1
            #Obstacles
            if 'obstacle' in obstacle.get_name():
                obs['obstacle'+str(o)+'_state'] = obstacle.get_state().copy()
                o += 1
        return obs

    def _reward(self) -> float:
        return 0.0
    
    def _end_episode(self) -> bool:
        collision = self.sim.collision_check()
        goal_reached = self.sim.goal_check()

        return collision or goal_reached, self._step >= self.max_episode_length
        
    def render(self):
        return self.sim.get_object_data()
    
    '''
    SCALING FUNCTIONS
    '''

    def _clip(
            self,
            action: list[float],
    ) -> list[float]:
        return np.multiply(self.max_ctrl,np.clip(action,a_min=-1,a_max=1))
    
    def _std(
            self,
            action: list[float],
    ) -> list[float]:
        if np.std(action) > 1:
            return np.multiply(self.max_ctrl,action/np.std(action))
        else:
            return np.multiply(self.max_ctrl,action)
    
    def _scale(
            self,
            action: list[float],
    ) -> list[float]:
        if abs(action).max > 1:
            return np.multiply(self.max_ctrl,action/np.linalg.norm(action))
        else:
            return np.multiply(self.max_ctrl,action)
        