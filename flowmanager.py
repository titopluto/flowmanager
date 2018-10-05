# Copyright (c) 2018 Maen Artimy
# 
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
# 
#   http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

from ryu.base import app_manager
from ryu.app.wsgi import WSGIApplication
from ryu.controller import dpset
# these are needed for the events
from ryu.controller import ofp_event
from ryu.controller.handler import HANDSHAKE_DISPATCHER
from ryu.controller.handler import CONFIG_DISPATCHER
from ryu.controller.handler import MAIN_DISPATCHER
from ryu.controller.handler import set_ev_cls

from ryu.ofproto import ofproto_v1_3
from ryu.lib import ofctl_v1_3
from ryu.lib import ofctl_utils
from ryu import utils

# for packet content
from ryu.lib.packet import packet
from ryu.lib.packet import ethernet
from ryu.lib.packet import ether_types

# for topology discovery
#from ryu.topology import event
from ryu.topology.api import get_all_switch, get_all_link, get_all_host

from webapi import WebApi
import os, logging
from logging.handlers import WatchedFileHandler
import datetime


class FlowManager(app_manager.RyuApp):
    #OFP_VERSIONS = [ofproto_v1_3.OFP_VERSION]

    _CONTEXTS = {'wsgi': WSGIApplication,
                 'dpset': dpset.DPSet}

    port_id = {
        "IN_PORT": 0xfffffff8,
        "TABLE": 0xfffffff9,
        "NORMAL": 0xfffffffa,
        "FLOOD": 0xfffffffb,
        "ALL": 0xfffffffc,
        "CONTROLLER": 0xfffffffd,
        "LOCAL": 0xfffffffe,
        "ANY": 0xffffffff
    }

    logname = 'flwmgr'
    logfile = 'flwmgr.log'

    def __init__(self, *args, **kwargs):
        super(FlowManager, self).__init__(*args, **kwargs)
        wsgi = kwargs['wsgi']
        self.dpset = kwargs['dpset']
        self.waiters = {}
        self.ofctl = ofctl_v1_3

        # Data exchanged with WebApi
        wsgi.register(WebApi,
                      {"webctl": self,
                       "dpset": self.dpset,
                       "waiters": self.waiters})

        self.reqfunction = {
            "switchdesc": self.ofctl.get_desc_stats,
            "portdesc": self.ofctl.get_port_desc,
            "portstat": self.ofctl.get_port_stats,
            "flowsumm": self.ofctl.get_aggregate_flow_stats,
            "tablestat": self.ofctl.get_table_stats,
            "queueconfig": self.ofctl.get_queue_config,
            "queuestat": self.ofctl.get_queue_stats,
            "meterstat": self.ofctl.get_meter_stats,
            "tablefeature": self.ofctl.get_table_features,
        }

        # Setup logging
        self.logger = self.get_logger(self.logname, self.logfile, 'INFO', 0)

    def get_logger(self, logname, logfile, loglevel, propagate):
        """Create and return a logger object."""
        # TODO: simplify
        logger = logging.getLogger(logname)
        logger_handler = WatchedFileHandler(logfile, mode='w')
        # removed \t%(name)-6s
        log_fmt = '%(asctime)s\t%(levelname)-8s\t%(message)s'
        logger_handler.setFormatter(
            logging.Formatter(log_fmt, '%b %d %H:%M:%S'))
        logger.addHandler(logger_handler)
        logger.propagate = propagate
        logger.setLevel(loglevel)
        return logger

    def get_switches(self):
        """Return switches."""
        return self.dpset.get_all()

    def get_stats(self, req, dpid):
        dp = self.dpset.get(int(str(dpid), 0))
        if req == "flows":
            return self.ofctl.get_flow_stats(dp, self.waiters)
        elif req == "groups":
           return {"desc": self.ofctl.get_group_desc(dp, self.waiters),
                     "stats": self.ofctl.get_group_stats(dp, self.waiters)}
        elif req == "meters":
           return {"desc": self.ofctl.get_meter_config(dp, self.waiters),
                    "stats": self.ofctl.get_meter_stats(dp, self.waiters)}

    def get_stats_request(self, request, dpid):
        """Get stats using ryu's api
        """
        dp = self.dpset.get(dpid)
        func = self.reqfunction.get(request, None)
        if dp and func:
            return func(dp, self.waiters)
        return None
            
    def read_logs(self):
        items = []
        with open(self.logfile, 'r') as my_file:
            while True:
                line = my_file.readline()
                if not line:
                    break
                lst = line.split('\t')
                items.append(lst)
                #items.append(line)
        return items

    def get_actions(self, parser, set):
        actions = []
        aDict = {
            'SET_FIELD': (parser.OFPActionSetField, 'field'),
            'COPY_TTL_OUT': (parser.OFPActionCopyTtlOut, None),
            'COPY_TTL_IN': (parser.OFPActionCopyTtlIn, None),
            'POP_PBB': (parser.OFPActionPopPbb, None),
            'PUSH_PBB': (parser.OFPActionPushPbb, 'ethertype'),
            'POP_MPLS': (parser.OFPActionPopMpls, 'ethertype'),
            'PUSH_MPLS': (parser.OFPActionPushMpls, 'ethertype'),
            'POP_VLAN': (parser.OFPActionPopVlan, None),
            'PUSH_VLAN': (parser.OFPActionPushVlan, 'ethertype'),
            'DEC_MPLS_TTL': (parser.OFPActionDecMplsTtl, None),
            'SET_MPLS_TTL': (parser.OFPActionSetMplsTtl, 'mpls_ttl'),
            'DEC_NW_TTL': (parser.OFPActionDecNwTtl, None),
            'SET_NW_TTL': (parser.OFPActionSetNwTtl, 'nw_ttl'),
            'SET_QUEUE': (parser.OFPActionSetQueue, 'queue_id'),
            'GROUP': (parser.OFPActionGroup, 'group_id'),
            'OUTPUT': (parser.OFPActionOutput, 'port'),
        }

        for action in set:
            key = action.keys()[0] #There should be only one key
            value = action[key]
            if key in aDict:
                f = aDict[key][0]       # the action
                if aDict[key][1]:       # check if the action needs a value
                    kwargs = {}
                    if aDict[key][1] == 'field':
                        x = value.split('=')
                        val = 0
                        if len(x)>1:
                            val = int(x[1]) if x[1].isdigit() else x[1]                         
                        kwargs = {x[0]: val}
                    elif aDict[key][1] == 'port':
                        x = value.upper()
                        val = self.port_id[x] if x in self.port_id else int(x)
                        kwargs = {aDict[key][1]: val}
                    else:
                        kwargs = {aDict[key][1]: int(value)}
                    actions.append(f(**kwargs))
                else:
                    actions.append(f())
            else:
                raise Exception("Action {} not supported!".format(key))
        return actions

    def process_flow_message(self, d):
        dpid = int(d.get("dpid", 0))
        dp = self.dpset.get(dpid)
        if not dp:
            return "Datapatch does not exist!"

        ofproto = dp.ofproto
        parser = dp.ofproto_parser

        command = {
            'add': ofproto.OFPFC_ADD,
            'mod': ofproto.OFPFC_MODIFY,
            'modst': ofproto.OFPFC_MODIFY_STRICT,
            'del': ofproto.OFPFC_DELETE,
            'delst': ofproto.OFPFC_DELETE_STRICT,
        }

        # Initialize arguments for the flow mod message
        msg_kwargs = {
            'datapath': dp,
            'command': command.get(d["operation"], ofproto.OFPFC_ADD),
            'buffer_id': ofproto.OFP_NO_BUFFER,
        }

        msg_kwargs['table_id'] = d.get('table_id', 0)

        # Match fields
        mf = d.get("match", None)
        # convert port names to numbers
        if "in_port" in mf:
            x = mf["in_port"]
            mf["in_port"] = self.port_id[x] if x in self.port_id else x
        # convert masks to tuples
        for f in mf:
            mask_pos = str(mf[f]).find('/')
            if mask_pos >=0:
                parts = mf[f].split('/')
                mf[f] = (parts[0], parts[1])
            if str(mf[f]).startswith('0x'):
                mf[f] = int(mf[f],16)

        msg_kwargs['match'] =  parser.OFPMatch(**mf) if mf else None

        msg_kwargs['hard_timeout'] = d.get('hard_timeout', 0)
        msg_kwargs['idle_timeout'] = d.get('idle_timeout', 0)
        msg_kwargs['priority'] = d.get('priority', 0)
        msg_kwargs['cookie'] = d.get('cookie', 0)
        msg_kwargs['cookie_mask'] = d.get('cookie_mask', 0)
        msg_kwargs['out_port'] = d.get('out_port', ofproto.OFPP_ANY)
        msg_kwargs['out_group'] = d.get('out_group', ofproto.OFPG_ANY)

        # instructions
        inst = []
        # Goto meter
        if ("meter_id" in d) and d['meter_id']:
            inst += [parser.OFPInstructionMeter(d["meter_id"])]
        # Apply Actions
        if ("apply" in d) and d["apply"]:
            applyActions = self.get_actions(parser, d["apply"])
            inst += [parser.OFPInstructionActions(
                ofproto.OFPIT_APPLY_ACTIONS, applyActions)]
        # Clear Actions
        if ("clearactions" in d) and d["clearactions"]:
            inst += [parser.OFPInstructionActions(
                ofproto.OFPIT_CLEAR_ACTIONS, [])]
        # Write Actions
        if ("write" in d) and d["write"]:
            # bc actions must be unique they are in dict
            # from dict to list
            toList = [{k:d["write"][k]} for k in d["write"]]
            #print(toList)
            writeActions = self.get_actions(parser, toList)
            inst += [parser.OFPInstructionActions(
                ofproto.OFPIT_WRITE_ACTIONS, writeActions)]
        # Write Metadata
        if ("metadata" in d) and d["metadata"]:
            meta_mask = d.get("metadata_mask", 0)
            inst += [parser.OFPInstructionWriteMetadata(
                d["metadata"], meta_mask)]
        # Goto Table Metadata
        if ("goto" in d) and d["goto"]:
            inst += [parser.OFPInstructionGotoTable(table_id=d["goto"])]

        msg_kwargs['instructions'] = inst

        # Flags
        flags = 0
        flags += 0x01 if d.get('SEND_FLOW_REM', False) else 0
        flags += 0x02 if d.get('CHECK_OVERLAP', False) else 0
        flags += 0x04 if d.get('RESET_COUNTS', False) else 0
        flags += 0x08 if d.get('NO_PKT_COUNTS', False) else 0
        flags += 0x10 if d.get('NO_BYT_COUNTS', False) else 0

        msg_kwargs['flags'] = flags

        # ryu/ryu/ofproto/ofproto_v1_3_parser.py
        msg = parser.OFPFlowMod(**msg_kwargs)
        try:
            dp.send_msg(msg)    # ryu/ryu/controller/controller.py
        except KeyError as e:
            return "Unrecognized field " + e.__repr__()
        except Exception as e:
            return "Error " + e.__repr__()

        return "Message sent successfully."

    def _prep_instructions(self, actions, ofproto, parser):
        # instructions
        inst = []
        apply_actions = []
        write_actions = []

        for item in actions:
            # Python 2 has both types
            if isinstance(item, unicode) or isinstance(item, str):
                if item.startswith('WRITE_METADATA'):
                    metadata = item.split(':')[1].split('/')
                    # expecting hex data
                    inst += [parser.OFPInstructionWriteMetadata(int(metadata[0], 16), int(metadata[1], 16))]
                elif item.startswith('GOTO_TABLE'):
                    table_id = int(item.split(':')[1])
                    inst += [parser.OFPInstructionGotoTable(table_id)]
                elif item.startswith('METER_ID'):
                    meter_id = int(item.split(':')[1])
                    inst += [parser.OFPInstructionMeter(meter_id)]
                elif item.startswith('CLEAR_ACTIONS'):
                    inst += [parser.OFPInstructionActions(ofproto.OFPIT_CLEAR_ACTIONS, [])]
                else:  # Apply Actions
                    action = item.split(':')
                    apply_actions += [{action[0]: action[1]}]

            elif isinstance(item, dict): # WRITE ACTIONS
                wractions = item["WRITE_ACTIONS"]
                for witem in wractions:
                    action = witem.split(':')
                    write_actions += [{action[0]: action[1]}]                    
        
        if apply_actions:
            applyActions = self.get_actions(parser, apply_actions)
            inst += [parser.OFPInstructionActions(
                ofproto.OFPIT_APPLY_ACTIONS, applyActions)]                    

        if write_actions:
            writeActions = self.get_actions(parser, write_actions)
            inst += [parser.OFPInstructionActions(
                ofproto.OFPIT_WRITE_ACTIONS, writeActions)]

        return inst

    def process_meter_upload(self, d):
        return "this feature is not supported yet."

    def process_group_upload(self, d):
        return "this feature is not supported yet."

    def process_flow_upload(self, d):
        """Sends flows to the switch to update flow tables.
        """
        switches = {str(t[0]):t[1] for t in self.get_switches()}
        for item in d:    # for each switch
            dpid = item.keys()[0]
            if dpid in switches.keys():

                dp = switches[dpid]
                ofproto = dp.ofproto
                parser = dp.ofproto_parser

                msg = parser.OFPFlowMod(dp,
                            table_id=ofproto.OFPTT_ALL,
                            command=ofproto.OFPFC_DELETE,
                            out_port=ofproto.OFPP_ANY,
                            out_group=ofproto.OFPG_ANY)

                dp.send_msg(msg)

                for flowentry in item[dpid]:
                    del flowentry['byte_count']
                    del flowentry['duration_sec']
                    del flowentry['duration_nsec']
                    del flowentry['packet_count']
                    del flowentry['length']


                    flowentry['datapath'] = dp
                    flowentry['command'] = ofproto.OFPFC_ADD
                    
                    mf = flowentry["match"]

                    # Quick and ugly fixes
                    # convert port names to numbers
                    if "in_port" in mf:
                        x = mf["in_port"]
                        mf["in_port"] = self.port_id[x] if x in self.port_id else x
                    # split address and mask
                    if "eth_dst" in mf:
                        if '/' in mf["eth_dst"]: # there is a mask
                            x = mf["eth_dst"].split('/')
                            mf["eth_dst"] = (x[0], x[1])
                    if "eth_src" in mf:
                        if '/' in mf["eth_src"]: # there is a mask
                            x = mf["eth_src"].split('/')
                            mf["eth_src"] = (x[0], x[1])


                    flowentry['match'] = parser.OFPMatch(**mf)

                    inst = self._prep_instructions(flowentry['actions'], ofproto, parser)
                    del flowentry['actions']
                    if inst:
                        flowentry['instructions'] = inst

                    try:
                        msg = parser.OFPFlowMod(**flowentry)
                    except Exception as e:
                        return "Value for '{}' is not found!".format(e.message)
                    
                    try:
                        dp.send_msg(msg)
                    except KeyError as e:
                        return e.__repr__()
                    except Exception as e:
                        return e.__repr__()

        return "Flows updated successfully."

    def process_group_message(self, d):
        """Sends group form data to the switch to update group tables.
        """

        dp = self.dpset.get(d["dpid"])
        if not dp:
            return "Datapatch does not exist!"

        ofproto = dp.ofproto
        parser = dp.ofproto_parser

        command = {
            'add': ofproto.OFPGC_ADD,
            'mod': ofproto.OFPGC_MODIFY,
            'del': ofproto.OFPGC_DELETE,
        }

        cmd = command.get(d["operation"], ofproto.OFPGC_ADD)

        type_convert = {'ALL': dp.ofproto.OFPGT_ALL,
                        'SELECT': dp.ofproto.OFPGT_SELECT,
                        'INDIRECT': dp.ofproto.OFPGT_INDIRECT,
                        'FF': dp.ofproto.OFPGT_FF}

        gtype = type_convert.get(d["type"])
        
        group_id = d["group_id"]

        buckets = []
        for bucket in  d["buckets"]:
            #print("bucket", bucket)
            weight = bucket.get('weight', 0)
            watch_port = bucket.get('watch_port', ofproto.OFPP_ANY)
            watch_group = bucket.get('watch_group', dp.ofproto.OFPG_ANY)
            actions = []
            if bucket['actions']:
                actions = self.get_actions(parser, bucket['actions'])
                buckets.append(dp.ofproto_parser.OFPBucket(
                    weight, watch_port, watch_group, actions))

        #print(dp, cmd, gtype, group_id, buckets)
        group_mod = parser.OFPGroupMod(
            dp, cmd, gtype, group_id, buckets)
        
        try:
            dp.send_msg(group_mod)    # ryu/ryu/controller/controller.py
        except KeyError as e:
            return e.__repr__()
        except Exception as e:
            return e.__repr__()
        
        return "Message sent successfully."

    def process_meter_message(self, d):
        """Sends meter form data to the switch to update meter table.
        """

        dp = self.dpset.get(d["dpid"])
        if not dp:
            return "Datapatch does not exist!"

        ofproto = dp.ofproto
        parser = dp.ofproto_parser

        command = {
            'add': ofproto.OFPMC_ADD,
            'mod': ofproto.OFPMC_MODIFY,
            'del': ofproto.OFPMC_DELETE,
        }
        cmd = command.get(d["operation"], ofproto.OFPMC_ADD)

        meter_id = d["meter_id"]

        flags = 0
        flags += 0x01 if d['OFPMF_KBPS'] else 0
        flags += 0x02 if d['OFPMF_PKTPS'] else 0
        flags += 0x04 if d['OFPMF_BURST'] else 0
        flags += 0x08 if d['OFPMF_STATS'] else 0

        # Flags must have KBPS or PKTPS
        flags = flags if (flags & 0x03) else (flags | 0x01)

        bands = []
        for band in  d["bands"]:
            #mtype = type_convert.get(band[0])
            if band[0] == 'DROP':
                bands += [parser.OFPMeterBandDrop(rate=band[1],
                    burst_size=band[2])]
            elif band[0] == 'DSCP_REMARK':
                bands += [parser.OFPMeterBandDscpRemark(rate=band[1], 
                    burst_size=band[2], prec_level=band[3])]

        # TODO: catch some errors
        meter_mod = parser.OFPMeterMod(dp, cmd, flags, meter_id, bands)
        try:    
            dp.send_msg(meter_mod)
        except KeyError as e:
            return e.__repr__()
        except Exception as e:
            return e.__repr__()
        
        return "Message sent successfully."

    # def get_flow_stats(self, req, dpid): # unused
    #     flow = {}  # no filters
    #     dp = self.dpset.get(int(str(dpid), 0))
    #     return self.ofctl.get_flow_stats(dp, self.waiters, flow)

    def get_packet_summary(self, content):
        pkt = packet.Packet(content)
        eth = pkt.get_protocols(ethernet.ethernet)[0]
        ethtype = eth.ethertype
        dst = eth.dst
        src = eth.src

        return '(src={}, dst={}, type=0x{:04x})'.format(src, dst, ethtype)

    ##### Event Handlers #######################################

    @set_ev_cls([  # ofp_event.EventOFPStatsReply,
        ofp_event.EventOFPDescStatsReply,
        ofp_event.EventOFPFlowStatsReply,
        ofp_event.EventOFPAggregateStatsReply,
        ofp_event.EventOFPTableStatsReply,
        # ofp_event.EventOFPTableFeaturesStatsReply,
        ofp_event.EventOFPPortStatsReply,
        # ofp_event.EventOFPQueueStatsReply,
        # ofp_event.EventOFPQueueDescStatsReply,
        ofp_event.EventOFPMeterStatsReply,
        ofp_event.EventOFPMeterFeaturesStatsReply,
        ofp_event.EventOFPMeterConfigStatsReply,
        ofp_event.EventOFPGroupStatsReply,
        # ofp_event.EventOFPGroupFeaturesStatsReply,
        ofp_event.EventOFPGroupDescStatsReply,
        ofp_event.EventOFPPortDescStatsReply,
    ], MAIN_DISPATCHER)
    def stats_reply_handler(self, ev):
        msg = ev.msg
        dp = msg.datapath

        if dp.id not in self.waiters:
            return
        if msg.xid not in self.waiters[dp.id]:
            return
        lock, msgs = self.waiters[dp.id][msg.xid]
        msgs.append(msg)

        flags = dp.ofproto.OFPMPF_REPLY_MORE

        if msg.flags & flags:
            return
        del self.waiters[dp.id][msg.xid]
        lock.set()

        # self.messages.append(msg)

    @set_ev_cls(ofp_event.EventOFPFlowRemoved, MAIN_DISPATCHER)
    def flow_removed_handler(self, ev):
        msg = ev.msg
        dp = msg.datapath
        ofp = dp.ofproto
        if msg.reason == ofp.OFPRR_IDLE_TIMEOUT:
            reason = 'IDLE TIMEOUT'
        elif msg.reason == ofp.OFPRR_HARD_TIMEOUT:
            reason = 'HARD TIMEOUT'
        elif msg.reason == ofp.OFPRR_DELETE:
            reason = 'DELETE'
        elif msg.reason == ofp.OFPRR_GROUP_DELETE:
            reason = 'GROUP DELETE'
        else:
            reason = 'unknown'

        self.logger.info('FlowRemoved\t'
                         'cookie=%d priority=%d reason=%s table_id=%d '
                         'duration_sec=%d duration_nsec=%d '
                         'idle_timeout=%d hard_timeout=%d '
                         'packet_count=%d byte_count=%d match.fields=%s',
                         msg.cookie, msg.priority, reason, msg.table_id,
                         msg.duration_sec, msg.duration_nsec,
                         msg.idle_timeout, msg.hard_timeout,
                         msg.packet_count, msg.byte_count, msg.match)

    @set_ev_cls(ofp_event.EventOFPErrorMsg,
                [HANDSHAKE_DISPATCHER, CONFIG_DISPATCHER, MAIN_DISPATCHER])
    def error_msg_handler(self, ev):
        msg = ev.msg

        self.logger.error('ErrorMsg\ttype=0x%02x code=0x%02x '
                          'message=%s',
                          msg.type, msg.code, utils.hex_array(msg.data))

    @set_ev_cls(ofp_event.EventOFPPacketIn, MAIN_DISPATCHER)
    def packet_in_handler(self, ev):
        msg = ev.msg
        dp = msg.datapath
        ofp = dp.ofproto
        if msg.reason == ofp.OFPR_NO_MATCH:
            reason = 'NO MATCH'
        elif msg.reason == ofp.OFPR_ACTION:
            reason = 'ACTION'
        elif msg.reason == ofp.OFPR_INVALID_TTL:
            reason = 'INVALID TTL'
        else:
            reason = 'UNKNOWN'

        self.logger.info('PacketIn\t'
                         'buffer_id=%x total_len=%d reason=%s '
                         'table_id=%d cookie=%d match=%s summary=%s',
                         msg.buffer_id, msg.total_len, reason,
                         msg.table_id, msg.cookie, msg.match,
                         #utils.hex_array(msg.data))
                         self.get_packet_summary(msg.data))

    # @set_ev_cls(event.EventSwitchEnter)
    def get_topology_data(self):
        """Get Topology Data
        """
        switch_list = get_all_switch(self)
        switches = [switch.to_dict() for switch in switch_list]
        links_list = get_all_link(self)
        links = [link.to_dict() for link in links_list]
        host_list = get_all_host(self)

        # To remove hosts that are not removed by controller
        ports = []
        for switch in switch_list:
           ports += switch.ports
        port_macs = [p.hw_addr for p in ports]
        n_host_list = [h for h in host_list if h.port.hw_addr in port_macs]

        hosts = [h.to_dict() for h in n_host_list]
            
        return {"switches": switches, "links":links, "hosts": hosts}


    def delete_flow_list(self, flowlist):
        for item in flowlist:
            item['operation'] = 'delst'
            result = self.process_flow_message(item)
  
        return 'Flows deleted successfully!'


