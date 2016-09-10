# Copyright (C) 2016 The OpenTimestamps developers
#
# This file is part of python-opentimestamps.
#
# It is subject to the license terms in the LICENSE file found in the top-level
# directory of this distribution.
#
# No part of python-opentimestamps including this file, may be copied,
# modified, propagated, or distributed except according to the terms contained
# in the LICENSE file.

import binascii
import hashlib

from opentimestamps.core.op import Op, UnaryOp, CryptOp, OpSHA256, OpAppend, OpPrepend
from opentimestamps.core.notary import TimeAttestation

import opentimestamps.core.serialize

class OpSet(dict):
    """Set of operations"""
    __slots__ = ['__make_timestamp']
    def __init__(self, make_timestamp_func):
        self.__make_timestamp = make_timestamp_func

    def add(self, key):
        """Add key

        Returns the value associated with that key
        """
        try:
            return self[key]
        except KeyError:
            value = self.__make_timestamp(key)
            self[key] = value
            return value

    def __setitem__(self, op, new_timestamp):
        try:
            existing_timestamp = self[op]
        except KeyError:
            dict.__setitem__(self, op, new_timestamp)
            return

        if existing_timestamp.msg != new_timestamp.msg:
            raise ValueError("Can't change existing result timestamp: timestamps are for different messages")

        dict.__setitem__(self, op, new_timestamp)

class Timestamp:
    """Proof that one or more attestations commit to a message

    The proof is in the form of a tree, with each node being a message, and the
    edges being operations acting on those messages. The leafs of the tree are
    attestations that attest to the time that messages in the tree existed prior.
    """
    __slots__ = ['__msg', 'attestations', 'ops']

    @property
    def msg(self):
        return self.__msg

    def __init__(self, msg):
        self.__msg = bytes(msg)
        self.attestations = set()
        self.ops = OpSet(lambda op: Timestamp(op(msg)))

    def __eq__(self, other):
        if isinstance(other, Timestamp):
            return self.__msg == other.__msg and self.ops == other.ops
        else:
            return False

    def __repr__(self):
        return 'Timestamp(<%s>)' % binascii.hexlify(self.__msg).decode('utf8')

    def merge(self, other):
        """Add all operations and attestations from another timestamp to this one

        Raises ValueError if the other timestamp isn't for the same message
        """
        if not isinstance(other, Timestamp):
            raise TypeError("Can only merge Timestamps together")

        if self.__msg != other.__msg:
            raise ValueError("Can't merge timestamps for different messages together")

        self.attestations.update(other.attestations)

        for other_op, other_op_stamp in other.ops.items():
            our_op_stamp = self.ops.add(other_op)
            our_op_stamp.merge(other_op_stamp)

    def serialize(self, ctx):
        if not len(self.attestations) and not len(self.ops):
            raise ValueError("An empty timestamp can't be serialized")

        sorted_attestations = sorted(self.attestations)
        if len(sorted_attestations) > 1:
            for attestation in sorted_attestations[0:-1]:
                ctx.write_bytes(b'\xff\x00')
                attestation.serialize(ctx)

        if len(self.ops) == 0:
            ctx.write_bytes(b'\x00')
            sorted_attestations[-1].serialize(ctx)

        elif len(self.ops) > 0:
            if len(sorted_attestations) > 0:
                ctx.write_bytes(b'\xff\x00')
                sorted_attestations[-1].serialize(ctx)

            sorted_ops = sorted(self.ops.items(), key=lambda item: item[0])
            for op, stamp in sorted_ops[0:-1]:
                ctx.write_bytes(b'\xff')
                op.serialize(ctx)
                stamp.serialize(ctx)

            last_op, last_stamp = sorted_ops[-1]
            last_op.serialize(ctx)
            last_stamp.serialize(ctx)

    @classmethod
    def deserialize(cls, ctx, initial_msg):
        """Deserialize

        Because the serialization format doesn't include the message that the
        timestamp operates on, you have to provide it so that the correct
        operation results can be calculated.
        """
        self = cls(initial_msg)

        def do_tag_or_attestation(tag):
            if tag == b'\x00':
                attestation = TimeAttestation.deserialize(ctx)
                self.attestations.add(attestation)

            else:
                op = Op.deserialize_from_tag(ctx, tag)
                stamp = Timestamp.deserialize(ctx, op(initial_msg))
                self.ops[op] = stamp

        tag = ctx.read_bytes(1)
        while tag == b'\xff':
            do_tag_or_attestation(ctx.read_bytes(1))

            tag = ctx.read_bytes(1)

        do_tag_or_attestation(tag)

        return self

    def all_attestations(self):
        """Iterate over all attestations recursively

        Returns iterable of (msg, attestation)
        """
        for attestation in self.attestations:
            yield (self.msg, attestation)

        for op_stamp in self.ops.values():
            yield from op_stamp.all_attestations()

    def str_tree(self, indent=0):
        """Convert to tree (for debugging)"""

        r = ""
        if len(self.attestations) > 0:
            for attestation in self.attestations:
                r += " "*indent + "verify %s" % str(attestation) + "\n"

        if len(self.ops) > 1:
            for op, timestamp in self.ops.items():
                r += " "*indent + " -> " + "%s"%str(op) + "\n"
                r += timestamp.str_tree(indent+4)
        elif len(self.ops) > 0:
            r += " "*indent + "%s\n" % str(tuple(self.ops.keys())[0])
            r += tuple(self.ops.values())[0].str_tree(indent)

        return r


class DetachedTimestampFile:
    """A file containing a timestamp for another file

    Contains a timestamp, along with a header and the digest of the file.
    """

    HEADER_MAGIC = b'\x00OpenTimestamps\x00\x00Proof\x00\xbf\x89\xe2\xe8\x84\xe8\x92\x94\x00'
    """Header magic bytes

    Designed to be give the user some information in a hexdump, while being
    identified as 'data' by the file utility.
    """

    MIN_FILE_DIGEST_LENGTH = 20 # 160-bit hash
    MAX_FILE_DIGEST_LENGTH = 32 # 256-bit hash

    @property
    def file_digest(self):
        """The digest of the file that was timestamped"""
        return self.timestamp.msg

    def __init__(self, file_hash_op, timestamp):
        self.file_hash_op = file_hash_op
        self.timestamp = timestamp

    def __repr__(self):
        return 'DetachedTimestampFile(<%s:%s>)' % (str(self.file_hash_op), binascii.hexlify(self.file_digest).decode('utf8'))

    def __eq__(self, other):
        return (self.__class__ == other.__class__ and
                self.file_hash_op == other.file_hash_op and
                self.timestamp == other.timestamp)

    @classmethod
    def from_fd(cls, file_hash_op, fd):
        fd_hash = file_hash_op.hash_fd(fd)
        return cls(file_hash_op, Timestamp(fd_hash))

    def serialize(self, ctx):
        ctx.write_bytes(self.HEADER_MAGIC)

        ctx.write_varbytes(self.timestamp.msg)
        self.file_hash_op.serialize(ctx)
        self.timestamp.serialize(ctx)

    @classmethod
    def deserialize(cls, ctx):
        header_magic = ctx.read_bytes(len(cls.HEADER_MAGIC))

        if header_magic != cls.HEADER_MAGIC:
            raise opentimestamps.core.serialize.BadMagicError(cls.HEADER_MAGIC, header_magic)

        file_hash = ctx.read_varbytes(cls.MAX_FILE_DIGEST_LENGTH, cls.MIN_FILE_DIGEST_LENGTH)
        file_hash_op = CryptOp.deserialize(ctx)
        timestamp = Timestamp.deserialize(ctx, file_hash)

        return DetachedTimestampFile(file_hash_op, timestamp)


def cat_then_unary_op(unary_op_cls, left, right):
    """Concatenate left and right, then perform a unary operation on them

    left and right can be either timestamps or bytes.

    Appropriate intermediary append/prepend operations will be created as
    needed for left and right.
    """
    if not isinstance(left, Timestamp):
        left = Timestamp(left)

    if not isinstance(right, Timestamp):
        right = Timestamp(right)

    left_append_stamp = left.ops.add(OpAppend(right.msg))
    right_prepend_stamp = right.ops.add(OpPrepend(left.msg))

    # Left and right should produce the same thing, so we can set the timestamp
    # of the left to the right.
    left.ops[OpAppend(right.msg)] = right_prepend_stamp

    return right_prepend_stamp.ops.add(unary_op_cls())


def cat_sha256(left, right):
    return cat_then_unary_op(OpSHA256, left, right)


def cat_sha256d(left, right):
    sha256_timestamp = cat_sha256(left, right)
    return sha256_timestamp.ops.add(OpSHA256())


def make_merkle_tree(timestamps, binop=cat_sha256):
    """Merkelize a set of timestamps

    A merkle tree of all the timestamps is built in-place using binop() to
    timestamp each pair of timestamps. The exact algorithm used is structurally
    identical to a merkle-mountain-range, although leaf sums aren't committed.
    As this function is under the consensus-critical core, it's guaranteed that
    the algorithm will not be changed in the future.

    Returns the timestamp for the tip of the tree.
    """

    stamps = timestamps
    while True:
        stamps = iter(stamps)

        try:
            prev_stamp = next(stamps)
        except StopIteration:
            raise ValueError("Need at least one timestamp")

        next_stamps = []
        for stamp in stamps:
            if prev_stamp is not None:
                next_stamps.append(cat_sha256(prev_stamp, stamp))
                prev_stamp = None
            else:
                prev_stamp = stamp

        if not next_stamps:
            return prev_stamp

        if prev_stamp is not None:
            next_stamps.append(prev_stamp)

        stamps = next_stamps
