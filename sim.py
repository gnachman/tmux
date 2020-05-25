#!/usr/bin/env python3

import random
import statistics
import warnings

def dlog(message):
    pass

class Queue:
    """Stores tuples of (length, time)."""
    def __init__(self):
        self.queue = []
        self.sum = 0

    def __repr__(self):
        return str(self.queue)

    def append(self, length, t):
        t = (length, t)
        self.queue.append(t)
        self.sum += length

    def is_empty(self):
        return len(self.queue) == 0
    def peek(self):
        return self.queue[0]

    def get(self, limit):
        """Reads up to `limit` bytes and returns (number of bytes read, timestamp)."""
        if not self.queue:
            return (0, None)
        value, t = self.queue[0]
        amount = value
        if value > limit:
            amount = limit
        length, time = self.queue[0]
        length -= amount
        self.queue[0] = (length, time)
        self.sum -= amount
        if length == 0:
            del self.queue[0]
        return (amount, t)

    def total(self):
        return self.sum

class Network:
    def __init__(self, name, throughput=500, latency=20):
        self.name = name
        self.throughput = throughput
        self.latency = latency
        # Timestamps in ingress and egress are the time the message entered the ingress queue.
        self.ingress = Queue()
        self.egress = Queue()

    def __repr__(self):
        return "Ingress: %s    Egress: %s" % (self.ingress, self.egress)
    def write(self, length, t):
        self.ingress.append(length, t)
        dlog("%s add %d to ingress" % (self.name, length))

    def update(self, now):
        """Move up to `throughput` bytes of age at least `latency` from ingress to egress."""
        available = self.throughput
        total = 0
        while available > 0 and not self.ingress.is_empty():
            length, time = self.ingress.peek()
            if now - time < self.latency:
                # Too new.
                break
            self.ingress.get(available)
            self.egress.append(length, time)
            available -= length
            total += length
            dlog("%s move %d to egress. now=%d time=%d latency=%d" % (self.name, length, now, time, self.latency))
        dlog("%s transmits %d bytes" % (self.name, total))

    def read(self):
        """Reads from the egress queue. Returns (timestamp, length) where
        timestamp is the send time of the oldest message read."""
        oldest = None
        sum = 0
        dlog("%s reading from egress queue: %s" % (self.name, self.egress))
        while not self.egress.is_empty():
            length, time = self.egress.get(self.throughput)
            if oldest is None:
                oldest = time
            sum += length
        return (oldest, sum)

class Terminal:
    def __init__(self, outbound, inbound):
        self.outbound = outbound
        self.inbound = inbound

    def update(self, now):
        time, length = self.inbound.read()
        if time is None:
            return
        dlog("read %d"% length)
        dlog("terminal acks %d bytes of age %d with timestamp %d" % (length, now - time, time))
        self.outbound.write(length, time)


class Controller:
    def __init__(self, target, now, window):
        self.target = target
        self.window = window
        self.prev = 2**32
        self.lats = []
        self.next = now
        self.alwaysSaturated = True
        self.status = ""

    def info(self):
        return self.status

    def update(self, latency, now, outstanding, lastAckTime, released):
        if latency is None:
            self.status = "no data"
            return 0
        if now < self.next:
            self.status = "not ready"
            # Not ready
            return 0

        if outstanding < self.window:
            # If the connection isn't always saturated, don't increase the
            # capacity. This prevents the capacity from growing without bound
            # while idle.
            self.alwaysSaturated = False

        self.lats.append(latency)
        if len(self.lats) < 10:
            self.status = "waiting"
            return 0

        mean = statistics.mean(self.lats)
        if mean > self.prev:
            self.status = "slow down"
            delta = -200
            self.next = now + 2 * latency
        elif mean <= self.prev and self.alwaysSaturated:
            self.status = "speed up"
            delta = 100
            self.next = now + 2 * latency
        else:
            self.status = "unsaturated"
            delta = 0
        self.prev = mean
        self.lats = []
        self.alwaysSaturated = True

        self.window += delta
        return delta



class Tmux:
    def __init__(self, outbound, inbound, controller):
        self.capacity = 128
        self.used = 0
        self.outbound = outbound
        self.inbound = inbound
        self.lastChange = None
        self.lastLatency = None
        self.controller = controller
        now = 0
        self.lastDelta = 0
        self.lastAckTime = None
        self.rand = random.Random(0)
        self.bytesWritten = 0
        self.bytesAcked = 0

    def readpty(self):
        return abs(self.rand.gauss(0, 50))

    def read_acks(self, now):
        """Reads acks, if any, and returns latency or None."""
        time, length = self.inbound.read()
        if time is None:
            self.bytesAcked = 0
            return None
        self.used = max(0, self.used - length)
        self.bytesAcked = length
        self.lastAckTime = time
        dlog("tmux receives acks for %d bytes, timestamp %d, increase available to %d" % (length, time, self.available()))
        return now - time

    def available(self):
        return max(0, self.capacity - self.used)

    def update(self, now):
        avail = self.available()
        used = self.used
        latency = self.read_acks(now)
        if latency:
            self.lastLatency = latency
            dlog("at time %d: tmux read ack of age %d with capacity %d" % (now, latency, self.capacity))

        delta = self.controller.update(latency, now, used, self.lastAckTime, self.bytesAcked)
        self.lastDelta = delta
        dlog("* send PID feedback latency of %s, it gives back %d" % (latency, delta))
        dlog("tmux adjusts capacity by %d" % delta)
        self.capacity = max(128, self.capacity + delta)

        new_bytes = 0
        for i in range(int(self.rand.uniform(1, 100))):
            if self.available() == 0:
                break
            n = min(self.available(), self.readpty())
            self.outbound.write(n, now)
            self.used += n
            new_bytes += n

        self.bytesWritten = new_bytes
        dlog("tmux writes %d capacity=%d/%d" % (new_bytes, self.used, self.capacity))

def log(values):
    s = ""
    for value in values:
        if type(value) == str:
            s += ("%15s" % value)
        elif value is None:
            s += ("%15s" % "-")
        else:
            s += ("%15.0f" % value)
    print(s)

outputConnection = Network("output", 500, 20)
ackConnection = Network("acks", float('inf'), 20)
terminal = Terminal(ackConnection, outputConnection)
controller = Controller(100, 0, 128)
tmux = Tmux(outputConnection, ackConnection, controller)
rand = random.Random(0)

now = 0
# Wrote: number of bytes tmux sends as %output
# Released: number of bytes acked by terminal
# Available: tmux's unused capacity
# Capacity: max number of bytes tmux is willing to have outstanding
# dCapacity: Change in capacity this iteration
# Queued: Number of bytes in buffers in the network
# Latency: Latency of acks received from terminal this iteration (time from write til ack)
# Ack time: The oldest timestamp of data acked this iteration
# Control: Status of the controller
# NetThru: Actual maximum network throughput
# NetLat: Actual network latency
log(["Time", "Wrote", "Released", "Available", "Capacity", "dCapacity", "Queued", "Latency", "Ack time", "Control", "NetThru", "NetLat"])
while True:
    dlog("-- Begin timestep %d --" % now)
    outputConnection.update(now)
    ackConnection.update(now)
    tmux.update(now)
    terminal.update(now)
    dlog(outputConnection)

    log([now, tmux.bytesWritten, tmux.bytesAcked, tmux.available(),
        tmux.capacity, tmux.lastDelta, outputConnection.ingress.total(),
        tmux.lastLatency, tmux.lastAckTime, controller.info(),
        outputConnection.throughput, outputConnection.latency])
    tmux.lastDelta = ""

    now += 20
    if now % 10000 == 0:
        outputConnection.throughput = int(rand.uniform(100, 10000))
        outputConnection.latency = int(rand.uniform(10, 100))
        print(" - update network -")

