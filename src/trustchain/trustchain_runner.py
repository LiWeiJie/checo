from twisted.internet import task
from base64 import b64encode
from Queue import Queue
from typing import Union
import random
import logging
from collections import defaultdict

from trustchain import TrustChain, TxBlock, CpBlock, Signature, Cons
from src.utils.utils import Replay, Handled, collate_cp_blocks, my_err_back
from src.utils.messages import SynMsg, SynAckMsg, AckMsg, SigMsg, CpMsg, ConsMsg


class RoundState:
    def __init__(self):
        self.received_cons = None
        self.received_sigs = {}
        self.received_cps = []

    def new_cons(self, cons):
        # type: (Cons) -> bool
        assert isinstance(cons, Cons)
        if self.received_cons is None:
            self.received_cons = cons
            return True

        # TODO eventually we need to store all received cons and check which are correctly and sufficiently signed
        assert cons == self.received_cons
        return False

    def new_sig(self, s):
        # type: (Signature) -> bool
        """
        :param s: 
        :return: True if it is new, otherwise False
        """
        assert isinstance(s, Signature)
        if s.vk not in self.received_sigs:
            self.received_sigs[s.vk] = s
            return True

        # TODO we should handle this
        assert self.received_sigs[s.vk] == s
        return False

    def new_cp(self, cp):
        # type: (CpBlock) -> None
        assert isinstance(cp, CpBlock)
        if len(self.received_cps) > 0:
            assert self.received_cps[0].round == cp.round
        self.received_cps.append(cp)


class TrustChainRunner:
    """
    We keep a queue of messages and handle them in order
    the handle function by itself essentially pushes the messages into the queue
    """
    def __init__(self, factory, msg_wrapper_f=lambda x: x):
        self.tc = TrustChain()
        self.factory = factory
        self.recv_q = Queue()
        self.send_q = Queue()  # only for syn messages
        self.msg_wrapper_f = msg_wrapper_f
        self.consensus_delay = factory.config.consensus_delay

        self.recv_lc = task.LoopingCall(self.process_recv_q)
        self.recv_lc.start(0.1, False).addErrback(my_err_back)

        self.send_lc = task.LoopingCall(self.process_send_q)
        self.send_lc.start(0.1, False).addErrback(my_err_back)

        self.collect_rubbish_lc = task.LoopingCall(self.collect_rubbish)
        self.collect_rubbish_lc.start(5, False).addErrback(my_err_back)

        self.log_tx_count_lc = task.LoopingCall(self._log_tx_count)
        self.log_tx_count_lc.start(5, False).addErrback(my_err_back)

        self.bootstrap_lc = None
        self.new_consensus_lc = None
        self.new_consensus_lc_count = 0
        self.cp_q = Queue()

        # states below are states used for negotiating transaction
        # TODO these states are really ugly and error prone,
        # TODO refactor them into another class, e.g. IntermediateTX
        # TODO the lock is also not ideal, prefer to chain up Deferred
        self.tx_locked = False  # only process one transaction at a time, otherwise there'll be hash pointer collisions
        self.block_r = None  # type: TxBlock
        self.tx_id = -1  # type: int
        self.src = None  # type: str
        self.s_s = None  # type: Signature
        self.m = None  # type: str
        self.prev_r = None  # type: str

        self.continuous_tx = False
        self.use_random_node_for_tx = False

        # attributes below are states for building new CP blocks
        self.round_states = defaultdict(RoundState)

        random.seed()

    def _log_tx_count(self):
        """
        Too much spam if we log all TX, thus use this in a LoopingCall
        :return: 
        """
        logging.info("TC: current tx count {}".format(self.tc.tx_count))

    def reset_state(self):
        self.tx_locked = False
        self.tx_id = -1  # the id of the transaction that we're processing
        self.block_r = None
        self.src = None
        self.s_s = None
        self.m = None
        self.prev_r = None

        while not self.cp_q.empty():
            r = self.cp_q.get()
            self.try_add_cp(r)

    def assert_unlocked_state(self):
        assert not self.tx_locked
        assert self.block_r is None
        assert self.src is None
        assert self.tx_id == -1
        assert self.s_s is None
        assert self.m is None
        assert self.prev_r is None

    def assert_after_syn_state(self):
        assert self.tx_locked
        assert self.block_r is None
        assert self.src is not None
        assert self.tx_id != -1
        assert self.s_s is None
        assert self.m is not None
        assert self.prev_r is None

    def assert_full_state(self):
        assert self.tx_locked
        assert self.block_r is not None
        assert self.src is not None
        assert self.tx_id != -1
        assert self.s_s is not None
        assert self.m is not None
        assert self.prev_r is not None

    def update_state(self, lock, block, tx_id, src, s_s, m, prev_r):
        self.tx_locked = lock
        self.block_r = block
        self.tx_id = tx_id
        self.src = src
        self.s_s = s_s
        self.m = m
        self.prev_r = prev_r

    def sufficient_sigs(self, r):
        if len(self.round_states[r].received_sigs) > self.factory.config.t:
            return True
        return False

    def collect_rubbish(self):
        for k in self.round_states.keys():
            if k < self.tc.latest_round:
                logging.debug("TC: pruning key {}".format(k))
                del self.round_states[k]

    def latest_promoters(self):
        r = self.tc.latest_round
        return self.tc.consensus[r].get_promoters(self.factory.config.n)

    def handle_cons_from_acs(self, msg):
        """
        This is only called after we get the output from ACS
        :param msg:
        :return:
        """
        bs, r = msg
        logging.debug("TC: handling cons from ACS {}, round {}".format(bs, r))

        if isinstance(bs, dict):
            assert len(bs) > 0
            assert isinstance(bs.values()[0][0], CpBlock)

            logging.debug("TC: adding cons")
            cons = Cons(r, collate_cp_blocks(bs))
            self.round_states[r].new_cons(cons)

            future_promoters = cons.get_promoters(self.factory.config.n)
            s = Signature(self.tc.vk, self.tc.sk, cons.hash)

            self.factory.gossip_except(future_promoters, ConsMsg(cons))
            self.factory.multicast(future_promoters, ConsMsg(cons))

            self.factory.gossip_except(future_promoters, SigMsg(s, r))
            self.factory.multicast(future_promoters, SigMsg(s, r))

            # we also try to add the CP here because we may receive the signatures before the actual CP
            self.try_add_cp(r)

        else:
            logging.debug("TC: not a dict type in handle_cons_from_acs")

    def handle_sig(self, msg, remote_vk):
        # type: (SigMsg, str) -> None
        assert isinstance(msg, SigMsg)

        logging.debug("TC: received SigMsg {} from {}".format(msg, b64encode(remote_vk)))
        if msg.r >= self.tc.latest_round:
            is_new = self.round_states[msg.r].new_sig(msg.s)
            if is_new:
                self.try_add_cp(msg.r)
                self.factory.gossip(msg)

    def handle_cp(self, msg, remote_vk):
        # type: (CpMsg, str) -> None
        assert isinstance(msg, CpMsg)

        logging.debug("TC: received CpMsg {} from {}".format(msg, b64encode(remote_vk)))
        if msg.r >= self.tc.latest_round:
            cp = msg.cp
            self.round_states[cp.round].new_cp(cp)

    def handle_cons(self, msg, remote_vk):
        # type: (ConsMsg, str) -> None
        assert isinstance(msg, ConsMsg)

        logging.debug("TC: received ConsMsg {} from {}".format(msg, b64encode(remote_vk)))
        if msg.r >= self.tc.latest_round:
            is_new = self.round_states[msg.cons.round].new_cons(msg.cons)
            if is_new:
                self.try_add_cp(msg.r)
                # call_later(1, self.factory.gossip, msg)
                self.factory.gossip(msg)

    def try_add_cp(self, r):
        """
        Try to add my own CP from the received consensus results and signatures
        The input parameter is a bit strange, we don't add the cp from the parameter, but from the buffer round_states
        we don't need lock here because this function runs atomically
        :param r: 
        :return: 
        """
        if self.tc.latest_round >= r:
            logging.debug("TC: already added the CP")
            return
        if self.round_states[r].received_cons is None:
            logging.debug("TC: don't have consensus result")
            return
        if not self.sufficient_sigs(r):
            logging.debug("TC: insufficient signatures")
            return

        if self.tx_locked:
            self.cp_q.put(r)
        else:
            self._add_cp(r)

    def _add_cp(self, r):
        """
        :param r:
        :return:
        """
        # here we create a new CP from the consensus result (both of round r)
        logging.debug("TC: adding CP in round {}".format(r))
        self.tc.new_cp(1,
                       self.round_states[r].received_cons,
                       self.round_states[r].received_sigs.values(),
                       self.factory.promoters)

        # new promoters are selected using the latest CP, these promoters are responsible for round r+1
        # no need to continue the ACS for earlier rounds
        assert r == self.tc.latest_round, "{} != {}"\
            .format(r, self.tc.latest_round)
        self.factory.promoters = self.latest_promoters()
        self.factory.acs.stop(self.tc.latest_round)

        assert len(self.factory.promoters) == self.factory.config.n, "{} != {}"\
            .format(len(self.factory.promoters), self.factory.config.n)
        logging.info('TC: CP count in Cons is {}'.format(self.tc.consensus[r].count))
        logging.info('TC: updated new promoters in round {} to [{}]'.format(
            r, ",".join(['"' + b64encode(p) + '"' for p in self.factory.promoters]))
        )

        # at this point the promoters are updated
        # finally collect new CP if I'm the promoter, otherwise send CP to promoter
        if self.tc.vk in self.factory.promoters:
            logging.info("TC: I'm a promoter, starting a new consensus round when we have enough CPs")
            self.round_states[r].new_cp(self.tc.my_chain.latest_cp)

            def try_start_acs(_msg, _r):
                self.new_consensus_lc_count += 1
                if self.tc.latest_round >= _r:
                    logging.debug("TC: somebody completed ACS before me, not starting")
                    # setting the following causes the old messages to be dropped
                    self.factory.acs.stop(self.tc.latest_round)
                    self.new_consensus_lc.stop()
                    self.new_consensus_lc_count = 0
                elif len(_msg) < self.factory.config.n and self.new_consensus_lc_count < 20:
                    # we don't have enough CPs to start the consensus, so wait for more until some timeout
                    pass
                else:
                    # the 200 here is a rough guess,
                    # it should result in a total of 1000 CPs if there are enough promoters
                    if self.factory.config.large_network and len(_msg) > 200:
                        cps = random.sample(_msg, 200)
                    else:
                        cps = _msg
                    self.factory.acs.reset_then_start(cps, _r)
                    self.new_consensus_lc.stop()
                    self.new_consensus_lc_count = 0

            assert self.new_consensus_lc_count == 0, "Overlapping ACS"
            self.new_consensus_lc = task.LoopingCall(try_start_acs, self.round_states[r].received_cps, r + 1)
            self.new_consensus_lc.start(self.consensus_delay, False).addErrback(my_err_back)
        else:
            logging.info("TC: I'm NOT a promoter")

        # send new CP to either all promoters
        self.factory.promoter_cast(CpMsg(self.tc.my_chain.latest_cp))

    def handle(self, msg, src):
        # type: (Union[SynMsg, SynAckMsg, AckMsg]) -> None
        logging.debug("TC: got message".format(msg))
        self.recv_q.put((msg, src))

    def process_recv_q(self):
        logging.debug("TC: processing recv_q, size: {}".format(self.recv_q.qsize()))
        qsize = self.recv_q.qsize()

        cnt = 0
        while not self.recv_q.empty() and cnt < qsize:
            cnt += 1
            msg, src = self.recv_q.get()

            if isinstance(msg, SynMsg):
                res = self.process_syn(msg, src)
                if isinstance(res, Replay):
                    self.recv_q.put((msg, src))

            elif isinstance(msg, SynAckMsg):
                res = self.process_synack(msg, src)
                if isinstance(res, Replay):
                    self.recv_q.put((msg, src))

            elif isinstance(msg, AckMsg):
                res = self.process_ack(msg, src)
                if isinstance(res, Replay):
                    self.recv_q.put((msg, src))

            else:
                raise AssertionError("Incorrect message type")

    def process_send_q(self):
        """
        Only process one at a time, because if the state gets locked after processing one until we receive SynAck
        :return: 
        """
        logging.debug("TC: processing send_q, size: {}".format(self.send_q.qsize()))
        if not self.send_q.empty():
            if self.tx_locked:
                return

            m, node = self.send_q.get()

            tx_id = random.randint(0, 2**31 - 1)
            msg = SynMsg(tx_id, self.tc.latest_hash, self.tc.next_h, m)

            self.update_state(True, None, tx_id, node, None, m, None)
            self.send(node, msg)

            logging.debug("TC: sent {} to node {}".format(msg, b64encode(node)))

    def send_syn(self, node, m):
        """
        puts the message into the queue for sending on a later time (when we're unlocked)
        we need to do this because we cannot start a transaction at any time, only when we're unlocked
        :param node:
        :param m:
        :return:
        """
        logging.debug('TC: putting {{ "node": {}, "m": {} }} in send_q'.format(b64encode(node), m))
        self.send_q.put((m, node))

    def process_syn(self, msg, src):
        # type: (SynMsg, str) -> Union[Handled, Replay]
        """
        I receive a syn, so I can initiate a block, but cannot seal it (missing signature)
        :param msg: message
        :param src: source/sender of the message
        :return:
        """
        logging.debug("TC: processing syn msg {}".format(msg))
        # put the processing in queue if I'm locked
        if self.tx_locked:
            logging.debug("TC: we're locked, putting syn in queue")
            return Replay()

        # we're not locked, so proceed
        logging.debug("TC: not locked, proceeding")
        tx_id = msg.tx_id
        prev_r = msg.prev
        h_r = msg.h  # height of receiver
        m = msg.m

        # make sure we're in the initial state
        self.assert_unlocked_state()
        block = TxBlock(self.tc.latest_hash, self.tc.next_h, h_r, m)  # generate s_s from this
        self.update_state(True,
                          block,
                          tx_id,
                          src,
                          block.sign(self.tc.vk, self.tc.sk),  # store my signature
                          m,
                          prev_r)
        self.send_synack()
        return Handled()

    def send_synack(self):
        self.assert_full_state()
        assert not self.block_r.is_sealed()
        msg = SynAckMsg(self.tx_id,
                        self.tc.latest_hash,
                        self.tc.next_h,
                        self.s_s)
        self.send(self.src, msg)

    def process_synack(self, msg, src):
        # type: (SynAckMsg, str) -> Union[Handled, Replay]
        """
        I should have all the information to make and seal a new tx block
        :param msg:
        :param src:
        :return:
        """
        logging.debug("TC: processing synack {} from {}".format(msg, b64encode(src)))
        tx_id = msg.tx_id
        prev_r = msg.prev
        h_r = msg.h
        s_r = msg.s
        assert tx_id == self.tx_id, "TC: not the tx_id we're expecting"

        self.assert_after_syn_state()  # we initiated the syn
        assert src == self.src
        assert src == s_r.vk

        logging.debug("TC: synack")
        self.block_r = TxBlock(self.tc.latest_hash, self.tc.next_h, h_r, self.m)
        s_s = self.block_r.sign(self.tc.vk, self.tc.sk)
        self.block_r.seal(self.tc.vk, s_s, src, s_r, prev_r)
        self.tc.new_tx(self.block_r)
        logging.debug("TC: added tx {}".format(self.block_r))

        self.send_ack(s_s)

        return Handled()

    def send_ack(self, s_s):
        # type: (Signature) -> None
        msg = AckMsg(self.tx_id, s_s)
        self.send(self.src, msg)

        self.reset_state()

        # running tx continuously, so we start again
        if self.continuous_tx:
            if self.use_random_node_for_tx:
                self._make_tx(self.factory.random_node_odd)
            else:
                self._make_tx(self.factory.neighbour)

    def process_ack(self, msg, src):
        # type: (AckMsg, str) -> Union[Handled, Replay]
        logging.debug("TC: processing ack {} from {}".format(msg, b64encode(src)))
        tx_id = msg.tx_id
        s_r = msg.s
        if tx_id != self.tx_id:
            logging.debug("TC: not the tx_id we're expecting, putting it back to queue")
            return Replay()

        assert src == self.src
        assert not self.block_r.is_sealed()

        logging.debug("TC: ack")
        self.block_r.seal(self.tc.vk, self.s_s, src, s_r, self.prev_r)
        self.tc.new_tx(self.block_r)
        logging.debug("TC: added tx {}".format(self.block_r))
        self.reset_state()

        return Handled()

    def send(self, node, msg):
        logging.debug("TC: sending {} to {}".format(self.msg_wrapper_f(msg), b64encode(node)))
        self.factory.send(node, self.msg_wrapper_f(msg))

    def make_tx_continuously(self, use_random_node=False):
        if use_random_node:
            if not self.factory.even_idx(self.tc.vk):
                return
            node = self.factory.random_node_odd
            self.use_random_node_for_tx = use_random_node

        else:
            node = self.factory.neighbour_if_even()
            if node is None:
                return
            assert node != self.factory.vk

        self.continuous_tx = True
        self._make_tx(node)

    def make_tx_periodically(self, interval, use_random_node=False):
        if use_random_node:
            if not self.factory.even_idx(self.tc.vk):
                return
            lc = task.LoopingCall(self._make_tx, self.factory.random_node_odd)

        else:
            node = self.factory.neighbour_if_even()
            if node is None:
                return
            assert node != self.factory.vk

            lc = task.LoopingCall(self._make_tx, node)

        lc.start(interval).addErrback(my_err_back)

    def _make_tx(self, node):
        """
        :return: 
        """
        if self.send_q.qsize() > 10 or self.recv_q.qsize() > 10:
            return

        # cannot be myself
        assert node != self.factory.vk

        # typical bitcoin tx is 500 bytes
        m = 'a' * random.randint(400, 600)
        logging.debug("TC: {} making tx to".format(b64encode(node)))
        self.send_syn(node, m)

    def bootstrap_promoters(self):
        """
        Assume all the nodes are already online, exchange genesis blocks, and start ACS.
        The first n values, sorted by vk, are promoters
        :return:
        """
        n = self.factory.config.n
        self.factory.promoters = sorted(self.factory.peers.keys())[:n]
        self.factory.promoter_cast(CpMsg(self.tc.genesis))

        def bootstrap_when_ready():
            if self.factory.vk in self.factory.promoters:
                logging.info("TC: bootstrap_lc, got {} CPs".format(len(self.round_states[0].received_cps)))
                # collect CPs of round 0, from it, create consensus result of round 1
                if len(self.round_states[0].received_cps) >= n:
                    if self.factory.config.large_network:
                        # picking only n CPs does not matter regarding fairness in the bootstrap stage
                        cps = random.sample(self.round_states[0].received_cps, n)
                    else:
                        cps = self.round_states[0].received_cps
                    self.factory.acs.start(cps, 1)
                    self.bootstrap_lc.stop()
            else:
                logging.info("TC: bootstrap_lc, not promoter, got {} CPs".format(len(self.round_states[0].received_cps)))
                self.bootstrap_lc.stop()

        self.bootstrap_lc = task.LoopingCall(bootstrap_when_ready)
        self.bootstrap_lc.start(5, False).addErrback(my_err_back)
