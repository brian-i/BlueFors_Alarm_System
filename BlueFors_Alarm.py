# -*- coding: utf-8 -*-
"""
===========================================
Program : BlueFors_Alarm_System/BlueFors_Alarm.py
===========================================
Summary: Sends out an email when pulse tubes are off,
         when the fridge's temperature is >= 50mK,
         when the gas handling pressures are too high
         (P4 >= 500 mBar or |P4 - P3| >= 50 mBar),
         or when the fridge logs stop updating for more
         than 3 minutes (server lost connection to the
         fridge instrumentation; re-pinged hourly while the
         outage lasts). A missing MC temperature
         log is excused if the last recorded MC temperature
         is below the sensor's lower limit (~7 mK) while the
         other logs are still updating.

         Launches program w/o GUI
"""
__author__ = ["Sadman Ahmed Shanto","Evangelos Vlachos","Clark Miyamoto","Brian Isakov"]

import csv
import logging
import math
import os
import signal
import time

from datetime import datetime

from BlueFors import BlueFors
from Emailer import Emailer
from Slack_Messenger import Slack_er

logging.getLogger('qcodes.instrument.instrument').setLevel(logging.ERROR)

class BlueForsAlarm:
    def __init__(self, folder_path, mc_channel_id=6, temp_threshold=0.05, wait_time=60,
                 channel_p3=3, channel_p4=4, p4_threshold=500.0, p4_p3_diff_threshold=50.0,
                 max_log_age=180, mc_underrange_threshold=0.007, logging_alarm_interval=3600):
        self.folder_path = folder_path
        self.mc_channel_id = mc_channel_id
        self.temp_threshold = temp_threshold
        self.wait_time = wait_time
        self.channel_p3 = channel_p3
        self.channel_p4 = channel_p4
        self.p4_threshold = p4_threshold
        self.p4_p3_diff_threshold = p4_p3_diff_threshold
        self.max_log_age = max_log_age
        self.mc_underrange_threshold = mc_underrange_threshold
        self.logging_alarm_interval = logging_alarm_interval
        # Tracks since when each log has been unreadable, and when the
        # logging alarm was last sent (reset when the logs are fresh again)
        self.log_stale_since = {}
        self.last_logging_alarm_time = None
        self.bf = BlueFors('bf_fridge',
                           folder_path=folder_path,
                           channel_vacuum_can=1,
                           channel_pumping_line=2,
                           channel_compressor_outlet=3,
                           channel_compressor_inlet=4,
                           channel_mixture_tank=5,
                           channel_venting_line=6,
                           channel_50k_plate=1,
                           channel_4k_plate=2,
                           channel_magnet=None,
                           channel_still=5,
                           channel_mixing_chamber=6)
        self.email_list = self.get_email_list()
        self.slack_channel = "fridge"
        self.slacker = Slack_er(self.bf)

    def get_subject(self, pt_off, mc_temp_high, p4_high, p4_p3_diff_high, temp, p3, p4):
        subject = []
        head_string = "🚨[URGENT]🚨"
        separator = " "

        subject.append(head_string)
        if pt_off:
            subject.append("BlueFors' Pulse Tubes are OFF!")
            subject.append(separator)
        if mc_temp_high:
            subject.append("Mixing chamber is WARM!")
            subject.append(separator)
        if p4_high:
            subject.append("Gas handling pressure P4 is too high!")
            subject.append(separator)
        if p4_p3_diff_high:
            subject.append("Pressure difference between P4 and P3 is too large!")
            subject.append(separator)

        tail_string = " Current temperature is {} K".format(temp)
        subject.append(tail_string)
        pressure_string = " (P3 = {} mBar, P4 = {} mBar)".format(p3, p4)
        subject.append(pressure_string)

        subject = "".join(subject)
        return subject

    def get_stale_logs(self, pulse_tube_status, mc_temp, p3, p4):
        '''
        Check that the pulse tube status, mixing chamber temperature and
        pressure logs are still being updated.

        A log counts as stale if it cannot be read at all (counted from
        the first failed read) or if its last entry is older than
        self.max_log_age seconds.

        Returns:
        - list of (log name, seconds since last proper update) tuples
        '''
        now = datetime.now()
        logs = [
            ('pulse tube status log', pulse_tube_status, self.bf.get_pulse_tube_status_timestamp()),
            ('mixing chamber temperature log', mc_temp, self.bf.get_temperature_timestamp(self.mc_channel_id)),
            ('pressure log', p3, self.bf.get_pressure_timestamp()),
        ]

        stale_logs = []
        for name, value, timestamp in logs:
            unreadable = timestamp is None \
                or (isinstance(value, float) and math.isnan(value)) \
                or value == -400 # -400 is the pulse tube status parse-error code
            if unreadable:
                since = self.log_stale_since.setdefault(name, now)
                age = (now - since).total_seconds()
            else:
                self.log_stale_since.pop(name, None)
                age = (now - timestamp).total_seconds()
            if age > self.max_log_age:
                stale_logs.append((name, age))

        # Edge case: the server stops logging temperatures outside the sensor
        # range (e.g. MC below ~7 mK), so the MC temperature log can go missing
        # while everything else keeps updating. If the last recorded MC
        # temperature dipped below the underrange threshold and the other logs
        # are still fresh, the missing/stale MC log is expected, not a lost
        # connection.
        stale_names = [name for name, _ in stale_logs]
        if 'mixing chamber temperature log' in stale_names \
                and 'pulse tube status log' not in stale_names \
                and 'pressure log' not in stale_names:
            last_mc_temp, last_mc_time = self.bf.get_most_recent_temperature(self.mc_channel_id)
            if last_mc_temp is not None \
                    and not (isinstance(last_mc_temp, float) and math.isnan(last_mc_temp)) \
                    and last_mc_temp < self.mc_underrange_threshold:
                print("MC temperature log not updating, but last recorded MC temp is "
                      "{:.4f} K at {} (< {:.3f} K, sensor underrange) - not counting as stale".format(
                          last_mc_temp, last_mc_time, self.mc_underrange_threshold))
                stale_logs = [(name, age) for name, age in stale_logs
                              if name != 'mixing chamber temperature log']

        return stale_logs

    def get_logging_alarm_message(self, stale_logs):
        descriptions = ", ".join("{} (no update for {:.0f} s)".format(name, age)
                                 for name, age in stale_logs)
        return "[URGENT] BlueFors logs are not updating - the server may have lost connection to the fridge control panel: {}".format(descriptions)

    def handle_logging_alarm(self, stale_logs):
        '''
        Ping Slack about stale/unavailable logs. The first ping goes out as
        soon as a log has been stale for more than self.max_log_age; while
        the outage continues, a reminder ping goes out once per
        self.logging_alarm_interval seconds. Resets when all logs are fresh.
        '''
        if not stale_logs:
            self.last_logging_alarm_time = None
            return
        now = datetime.now()
        if self.last_logging_alarm_time is None \
                or (now - self.last_logging_alarm_time).total_seconds() >= self.logging_alarm_interval:
            self.send_slack_message(self.get_logging_alarm_message(stale_logs))
            self.last_logging_alarm_time = now

    def get_email_list(self):
        with open('emails.csv') as f:
            reader = csv.reader(f)
            email_list = list(reader)[0]
        return email_list

    def get_content(self, content="This message has been automatically generated by BlueFors_Alarm script from Emailer Bot."):
        return content

    def send_slack_message(self, message):
        slack_message = '<!channel> ' + message
        self.slacker.send_message(self.slack_channel, slack_message)

    def start_alarm_system(self):
        '''
        Call this function to start the alarm system.

        Returns:
        - None
        '''
        # Register the signal handlers
        signal.signal(signal.SIGINT, self.signal_handler)  # Handle Control+C
        signal.signal(signal.SIGTERM, self.signal_handler)  # Handle Control+Z

        # Send message that the alarm system is activated
        self.send_slack_message('🔔 BlueFors Alarm is now active and monitoring logs.')

        while True:
            pulse_tube_status = self.bf.get_pulse_tube_status()
            mc_temp = self.bf.get_temperature(self.mc_channel_id)
            p3 = self.bf.get_pressure(self.channel_p3)
            p4 = self.bf.get_pressure(self.channel_p4)

            print("Pulse Tube Status: {}".format(pulse_tube_status))
            print("MC Temperature: {} K".format(mc_temp))
            print("P3: {} mBar".format(p3))
            print("P4: {} mBar".format(p4))

            pulse_tube_off = bool(pulse_tube_status == 0)
            mc_temp_high = bool(mc_temp >= self.temp_threshold)
            # NaN pressures (e.g. missing log file) compare False, so they never trigger the alarm
            p4_high = bool(p4 >= self.p4_threshold)
            p4_p3_diff_high = bool(abs(p4 - p3) >= self.p4_p3_diff_threshold)

            if pulse_tube_off or mc_temp_high or p4_high or p4_p3_diff_high: # Triggers if something is wrong w/ fridge
                # Send out an email
                subject = self.get_subject(pulse_tube_off, mc_temp_high, p4_high, p4_p3_diff_high, mc_temp, p3, p4)
                content = self.get_content()

                # Send message to Slack
                self.send_slack_message(subject)

                #break

            # Separate alarm if the logs themselves stop updating (e.g. server lost connection to the fridge)
            stale_logs = self.get_stale_logs(pulse_tube_status, mc_temp, p3, p4)
            if stale_logs:
                print("WARNING: stale/unavailable logs: {}".format(stale_logs))
            self.handle_logging_alarm(stale_logs) # First ping after max_log_age, then once per logging_alarm_interval

            print("Waiting for next reading...")
            time.sleep(self.wait_time)

    def alarm_system_deactivated(self):
        page_link = None
        self.send_slack_message(f'🔕 BlueFors Alarm is now deactivated.')

    def signal_handler(self, signum, frame):
        self.alarm_system_deactivated()
        exit(1)

if __name__=='__main__':
    folder_path = r'C:\Users\gyeni\Desktop\Bluefors logs'
    alarm = BlueForsAlarm(folder_path)
    alarm.start_alarm_system()
    alarm.alarm_system_deactivated()