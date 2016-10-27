# -*- coding: utf-8 -*-
"""
    lantz.drivers.newport.fsm300
    ~~~~~~~~~~~~~~~~~~~~~~~~~~~~

    Implementation of FSM300 using NI DAQ controller

    Author: Kevin Miao
    Date: 9/27/2016
"""

from lantz import Driver
from lantz.driver import Feat, DictFeat, Action
from lantz.drivers.ni.daqmx import AnalogOutputTask, VoltageOutputChannel

from lantz import Q_

import time

import numpy as np

def enforce_point_units(point, units='um'):
    x, y = point
    if not isinstance(x, Q_):
        x = Q_(x, 'um')
    if not isinstance(y, Q_):
        y = Q_(y, 'um')
    point = x, y
    return point


class FSM300(Driver):

    def __init__(self, x_ao_ch, y_ao_ch,
                 ao_smooth_rate=Q_('10 kHz'), ao_smooth_steps=Q_('1000 1/V'),
                 limits=((Q_(-10, 'V'), Q_(10, 'V')), (Q_(-10, 'V'), Q_(10, 'V'))),
                 cal=(Q_(9.5768, 'um/V'), Q_(7.1759, 'um/V'))):
        x_limits_mag = tuple(float(val / Q_('1 V')) for val in limits[0])
        y_limits_mag = tuple(float(val / Q_('1 V')) for val in limits[1])
        self.task = AnalogOutputTask('fsm300')
        VoltageOutputChannel(x_ao_ch, name='fsm_x', min_max=x_limits_mag, units='volts', task=self.task)
        VoltageOutputChannel(y_ao_ch, name='fsm_y', min_max=y_limits_mag, units='volts', task=self.task)
        self.ao_smooth_rate = ao_smooth_rate
        self.ao_smooth_steps = ao_smooth_steps
        self.cal = cal

        self._position = (Q_('0 um'), Q_('0 um'))

        super().__init__()

        return

    def ao_smooth_func(self, init_point, final_point):
        init_x, init_y = init_point
        final_x, final_y = final_point

        init_x_voltage, final_x_voltage = init_x / self.cal[0], final_x / self.cal[0]
        init_y_voltage, final_y_voltage = init_y / self.cal[1], final_y / self.cal[1]
        diff_x_voltage = final_x_voltage - init_x_voltage
        diff_y_voltage = final_y_voltage - init_y_voltage

        diff_voltage = max(abs(diff_x_voltage), abs(diff_y_voltage))
        steps = int(np.ceil(diff_voltage * self.ao_smooth_steps))
        init = np.array([val.to('V').magnitude for val in [init_x_voltage, init_y_voltage]])
        diff = np.array([val.to('V').magnitude for val in [diff_x_voltage, diff_y_voltage]])

        versine_steps = (1.0 - np.cos(np.linspace(0.0, np.pi, steps))) / 2.0

        step_voltages = np.outer(np.ones(steps), init) + np.outer(versine_steps, diff)
        return step_voltages

    def ao_linear_func(self, init_point, final_point, steps):
        init_x, init_y = init_point
        final_x, final_y = final_point

        init_x_voltage, final_x_voltage = init_x / self.cal[0], final_x / self.cal[0]
        init_y_voltage, final_y_voltage = init_y / self.cal[1], final_y / self.cal[1]
        diff_x_voltage = final_x_voltage - init_x_voltage
        diff_y_voltage = final_y_voltage - init_y_voltage

        diff_voltage = max(abs(diff_x_voltage), abs(diff_y_voltage))
        init = np.array([val.to('V').magnitude for val in [init_x_voltage, init_y_voltage]])
        diff = np.array([val.to('V').magnitude for val in [diff_x_voltage, diff_y_voltage]])

        linear_steps = np.linspace(0.0, 1.0, steps)

        step_voltages = np.outer(np.ones(steps), init) + np.outer(linear_steps, diff)
        return step_voltages

    @Feat()
    def abs_position(self):
        return self._position


    @abs_position.setter
    def abs_position(self, point):
        point = enforce_point_units(point)
        step_voltages = self.ao_smooth_func(self._position, point)
        if step_voltages.size:
            steps = step_voltages.shape[0]
            clock_config = {
                'source': 'OnboardClock',
                'rate': self.ao_smooth_rate.to('Hz').magnitude,
                'sample_mode': 'finite',
                'samples_per_channel': steps,
            }
            self.task.configure_timing_sample_clock(**clock_config)
            task_config = {
                'data': step_voltages,
                'auto_start': False,
                'timeout': 0,
                'group_by': 'scan',
            }
            self.task.write(**task_config)
            self.task.start()
            time.sleep((steps / self.ao_smooth_rate).to('s').magnitude)
            self.task.stop()
        self._position = point

    @Action()
    def line_scan(self, init_point, final_point, steps, acq_task, acq_rate=Q_('10 kHz'), pts_per_pos=100):
        init_point = enforce_point_units(init_point)
        final_point = enforce_point_units(final_point)
        step_voltages = self.ao_linear_func(init_point, final_point, steps)
        step_voltages = np.repeat(step_voltages, pts_per_pos, axis=0)

        if acq_task.IO_TYPE == 'CI':

            # add extra sample for taking diff
            clock_config = {
                #'source': '/Dev1/ao/SampleClock',
                'rate': acq_rate.to('Hz').magnitude,
                'sample_mode': 'finite',
                'samples_per_channel': len(step_voltages) + 1,
            }
            self.task.configure_timing_sample_clock(**clock_config)
            clock_config = {
                'source': '/Dev1/ao/SampleClock',
                'rate': acq_rate.to('Hz').magnitude,
                'sample_mode': 'finite',
                'samples_per_channel': len(step_voltages) + 1,
            }
            acq_task.configure_timing_sample_clock(**clock_config)
            task_config = {
                'data': step_voltages,
                'auto_start': False,
                'timeout': 0,
                'group_by': 'scan',
            }
            acq_task.arm_start_trigger_source = '/Dev1/ao/StartTrigger'
            acq_task.arm_start_trigger_type = 'digital_edge'
            acq_task.start()

            self.task.write(**task_config)

            self.task.start()
            time.sleep(len(step_voltages)/acq_rate.to('Hz').magnitude)
            #acq_task.arm_start_trigger_source = 'Dev1/PFI15'
            #acq_task.arm_start_trigger_type = 'digital_edge'

            scanned = acq_task.read(samples_per_channel=len(step_voltages)+1,
                                    timeout=(len(step_voltages) + 1)/acq_rate.to('Hz').magnitude)

            #delta_cts = np.insert(np.diff(scanned), scanned[0], 0)
            delta_cts = np.diff(scanned)
            rate = delta_cts * acq_rate.to('Hz').magnitude
            acq_task.stop()
            self.task.stop()
            nb_chan = scanned.shape[0]

            # TODO: check to make sure that this reshapes the way it should
            return rate.reshape((pts_per_pos,steps))

        else:

            clock_config = {
            'source': 'OnboardClock',
            'rate': acq_rate.to('Hz').magnitude,
            'sample_mode': 'finite',
            'samples_per_channel': len(step_voltages),
            }
            self.task.configure_timing_sample_clock(**clock_config)
            acq_task.configure_timing_sample_clock(**clock_config)
            task_config = {
                'data': step_voltages,
                'auto_start': False,
                'timeout': 0,
                'group_by': 'scan',
            }
            self.task.write(**task_config)
            self.task.configure_trigger_digital_edge_start('ai/StartTrigger')
            self.task.start()
            acq_task.start()
            scanned = acq_task.read(samples_per_channel=len(step_voltages))
            acq_task.stop()
            self.task.stop()
            nb_chan = scanned.shape[0]
            return scanned.reshape((nb_chan,steps,pts_per_pos)).mean(axis=2)
