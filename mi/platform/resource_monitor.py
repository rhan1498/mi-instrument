#!/usr/bin/env python

"""
@package ion.agents.platform.resource_monitor
@file    ion/agents/platform/resource_monitor.py
@author  Carlos Rueda
@brief   Platform resource monitoring for a set of attributes having same rate
"""

__author__ = 'Carlos Rueda'
__license__ = 'Apache 2.0'


from pyon.public import log

from pyon.util.containers import current_time_millis

from ion.agents.platform.platform_driver_event import AttributeValueDriverEvent
from ion.agents.platform.util import ntp_2_ion_ts

import logging
from gevent import Greenlet, sleep

import pprint


# Platform attribute values are reported for the stream name "parsed".
# TODO confirm this.
#_STREAM_NAME = "parsed"
_STREAM_NAME = "sec_node_eng_data"
# A small "ION System time" compliant increment to the latest received timestamp
# for purposes of the next request so we don't get that last sample repeated.
# Since "ION system time" is in milliseconds, this delta is in milliseconds.
_DELTA_TIME = 10

# _MULT_INTERVAL: for any request, the from_time parameter will be at most
# _MULT_INTERVAL * _rate_secs in the past wrt current time (OOIION-1372):
_MULT_INTERVAL = 3


class ResourceMonitor(object):
    """
    Monitor for specific attributes having the same nominal monitoring rate.
    """

    def __init__(self, platform_id, rate_secs, attr_defns,
                 get_attribute_values, notify_driver_event):
        """
        Creates a monitor for a specific attribute in a given platform.
        Call start to start the monitoring greenlet.

        @param platform_id Platform ID
        @param rate_secs   Monitoring rate in secs
        @param attr_defns  List of attribute definitions
        @param get_attribute_values
                           Function to retrieve attribute values for the specific
                           platform, to be called like this:
                               get_attribute_values(attr_ids, from_time)
        @param notify_driver_event
                           Callback to notify whenever a value is retrieved.
        """
        log.debug("%r: ResourceMonitor entered. rate_secs=%s, attr_defns=%s",
                  platform_id, rate_secs, attr_defns)

        self._get_attribute_values = get_attribute_values
        self._platform_id = platform_id
        self._rate_secs = rate_secs
        self._attr_defns = attr_defns
        self._notify_driver_event = notify_driver_event

        # corresponding attribute IDs to be retrieved
        self._attr_ids = []
        # and "ION System time" compliant timestamp of last retrieved value for
        # each attribute:
        self._last_ts_millis = {}
        
#        self._attr_id_to_ion_parameter_name = {}

        for attr_defn in self._attr_defns:
            if 'ion_parameter_name' in attr_defn:
                attr_id = attr_defn['ion_parameter_name']
                self._attr_ids.append(attr_id)
                self._last_ts_millis[attr_id] = None
            else:
                log.warn("%r: 'attr_id' key expected in attribute definition: %s",
                         self._platform_id, attr_defn)

        self._active = False

        # for debugging purposes
        self._pp = pprint.PrettyPrinter()

        log.debug("%r: ResourceMonitor created. rate_secs=%s, attr_ids=%s",
                  platform_id, rate_secs, self._attr_ids)

    def __str__(self):
        return "%s{platform_id=%r; rate_secs=%s; attr_ids=%s}" % (
            self.__class__.__name__,
            self._platform_id, self._rate_secs, str(self._attr_ids))

    def start(self):
        """
        Starts greenlet for resource monitoring.
        """
        log.debug("%r: starting resource monitoring %s", self._platform_id, self)
        self._active = True
        runnable = Greenlet(self._run)
        runnable.start()

    def _run(self):
        """
        The target function for the greenlet.
        """
        while self._active:
            slept = 0
            # loop to incrementally sleep up to rate_secs while promptly
            # reacting to request for termination
            while self._active and slept < self._rate_secs:
                # sleep in increments of 0.5 secs
                incr = min(0.5, self._rate_secs - slept)
                sleep(incr)
                slept += incr

            if self._active:
                try:
                    self._retrieve_attribute_values()
                except Exception:
                    log.exception("exception in _retrieve_attribute_values")

        log.debug("%r: monitoring greenlet stopped. rate_secs=%s; attr_ids=%s",
                  self._platform_id, self._rate_secs, self._attr_ids)

    def _retrieve_attribute_values(self):
        """
        Retrieves the attribute values using the given function and calls
        _values_retrieved.
        """

        # TODO: note that the "from_time" parameters for the request below
        # as well as the expected response are influenced by the RSN case
        # (see CI-OMS interface). Need to see whether it also applies to
        # CGSN so eventually adjustments may be needed.
        #

        # note that the "from_time" parameter in each pair (attr_id, from_time)
        # for the _get_attribute_values call below, is in millis in UNIX epoch.

        curr_time_millis = current_time_millis()

        # minimum value for the from_time parameter (OOIION-1372):
        min_from_time = curr_time_millis - 1000 * _MULT_INTERVAL * self._rate_secs
        # this corresponds to (_MULT_INTERVAL * self._rate_secs) ago.

        # determine each from_time for the request:
        attrs = []
        for attr_id in self._attr_ids:
            if self._last_ts_millis[attr_id] is None:
                # Very first request for this attribute. Use min_from_time:
                from_time = min_from_time

            else:
                # We've already got values for this attribute. Use the latest
                # timestamp + _DELTA_TIME as a basis for the new request:
                from_time = int(self._last_ts_millis[attr_id]) + _DELTA_TIME

                # but adjust it if it goes too far in the past:
                if from_time < min_from_time:
                    from_time = min_from_time

            log.trace("test_resource_monitoring_recent: attr_id=%s, from_time=%s, %s millis ago",
                      attr_id, from_time, curr_time_millis - from_time)

            attrs.append((attr_id, from_time))
            

        log.debug("%r: _retrieve_attribute_values: attrs=%s",
                  self._platform_id, attrs)

        retrieved_vals = self._get_attribute_values(attrs)

        if retrieved_vals is None:
            # lost connection; nothing else to do here:
            return

        good_retrieved_vals = {}

        # do validation: we expect an array of tuples (val, timestamp) for
        # each attribute. If not, log a warning for the attribute and
        # continue processing with the other valid attributes:
        for attr_id, vals in retrieved_vals.iteritems():
            if not isinstance(vals, (list, tuple)):
                log.warn("%r: expecting an array for attribute %r, but got: %r",
                         self._platform_id, attr_id, vals)
                continue

            if len(vals):
                if not isinstance(vals[0], (tuple, list)):
                    log.warn("%r: expecting elements in array to be tuples "
                             "(val, ts) for attribute %r, but got: %r",
                             self._platform_id, attr_id, vals[0])
                    continue

            good_retrieved_vals[attr_id] = vals  # even if empty array.

        if not good_retrieved_vals:
            # nothing else to do.  TODO perhaps an additional warning?
            return

        if log.isEnabledFor(logging.TRACE):  # pragma: no cover
            # show good_retrieved_vals as retrieved (might be large)
            log.trace("%r: _retrieve_attribute_values: _get_attribute_values "
                      "for attrs=%s returned\n%s",
                      self._platform_id, attrs, self._pp.pformat(good_retrieved_vals))
        elif log.isEnabledFor(logging.DEBUG):  # pragma: no cover
            summary = {attr_id: "(%d vals)" % len(vals)
                       for attr_id, vals in good_retrieved_vals.iteritems()}
            log.debug("%r: _retrieve_attribute_values: _get_attribute_values "
                      "for attrs=%s returned %s",
                      self._platform_id, attrs, summary)

        # vals_dict: attributes with non-empty reported values:
        vals_dict = {}
        for attr_id, from_time in attrs:
            if not attr_id in good_retrieved_vals:
                log.warn("%r: _retrieve_attribute_values: unexpected: "
                         "response does not include requested attribute %r. "
                         "Response is: %s",
                         self._platform_id, attr_id, good_retrieved_vals)
                continue

            attr_vals = good_retrieved_vals[attr_id]
            if not attr_vals:
                log.debug("%r: No values reported for attribute=%r from_time=%f",
                          self._platform_id, attr_id, from_time)
                continue

            if log.isEnabledFor(logging.DEBUG):
                self._debug_values_retrieved(attr_id, attr_vals)

            # ok, include this attribute for the notification:
            vals_dict[attr_id] = attr_vals

        if vals_dict:
            self._values_retrieved(vals_dict)

    def _values_retrieved(self, vals_dict):
        """
        A values response has been received. Create and notify
        corresponding event to platform agent.
        """

        # update _last_ts_millis for each retrieved attribute:
        for attr_id, attr_vals in vals_dict.iteritems():

            _, ntp_ts = attr_vals[-1]

            # update _last_ts_millis based on ntp_ts: note that timestamps are reported
            # in NTP so we need to convert it to ION system time for a subsequent request:
            self._last_ts_millis[attr_id] = ntp_2_ion_ts(ntp_ts)

        # finally, notify the values event:
        driver_event = AttributeValueDriverEvent(self._platform_id,
                                                 _STREAM_NAME,
                                                 vals_dict)
        self._notify_driver_event(driver_event)

    def _debug_values_retrieved(self, attr_id, values): # pragma: no cover
        ln = len(values)
        # just show a couple of elements
        arrstr = "["
        if ln <= 3:
            vals = [str(e) for e in values[:ln]]
            arrstr += ", ".join(vals)
        else:
            vals = [str(e) for e in values[:2]]
            last_e = values[-1]
            arrstr += ", ".join(vals)
            arrstr += ", ..., " + str(last_e)
        arrstr += "]"
        log.debug("%r: attr=%r: values retrieved(%s) = %s",
                  self._platform_id, attr_id, ln, arrstr)

    def stop(self):
        log.debug("%r: stopping resource monitoring %s", self._platform_id, self)
        self._active = False
