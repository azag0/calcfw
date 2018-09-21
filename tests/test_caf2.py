# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at http://mozilla.org/MPL/2.0/.
import os
import json
from pathlib import Path

import caf2 as caf
from caf2.files import bash
from caf2.json_utils import ClassJSONDecoder, ClassJSONEncoder

debug_level = os.environ.get('CAF_DEBUG')
if debug_level:
    import logging
    logging.basicConfig()
    logging.getLogger('caf2').setLevel(int(debug_level))


@caf.Rule
def total(xs):
    return sum(xs)


@caf.Rule
def add(x, y):
    return total([x, y])


def test_pass_through():
    with caf.Session() as sess:
        assert sess.eval(10) == 10


def test_fibonacci():
    @caf.Rule
    def fib(n):
        if n < 2:
            return n
        return total([fib(n-1), fib(n-2)])

    with caf.Session() as sess:
        assert sess.eval(fib(10)) == 55


def test_fibonacci2():
    @caf.Rule
    def fib(n):
        if n < 2:
            return n
        return add(fib(n-1), fib(n-2))

    with caf.Session() as sess:
        assert sess.eval([fib(5), fib(10)]) == [5, 55]


def test_fibonacci3():
    @caf.Rule
    def fib(n):
        if n < 2:
            return [[n]]
        return [[add(fib(n-1)[0][0], fib(n-2)[0][0])]]

    with caf.Session() as sess:
        assert sess.eval(fib(10)[0][0]) == 55


def test_calc():
    @caf.Rule
    def setup():
        return [(
            dist,
            bash(
                'expr $(cat input) "*" 2; true'.encode(),
                {'input': str(dist).encode()},
            )['STDOUT']
        ) for dist in range(5)]

    @caf.Rule
    def analysis(results):
        return next(dist for dist, res in results if int(res) == 6)

    with caf.Session() as sess:
        assert sess.eval(analysis(setup())) == 3


def test_json_utils():
    class MyClass:
        def __init__(self, x):
            self.x = x

        def __hash__(self):
            return hash(self.x)

        def __eq__(self, other):
            return self.x == other.x

    obj = {'x': MyClass(1), 'ys': [MyClass(2)]}
    tape = set()
    jsonstr = json.dumps(
        obj,
        tape=tape,
        classes={MyClass: lambda x: {'x': x.x}},
        cls=ClassJSONEncoder
    )
    assert len(tape) == 2
    obj2 = json.loads(
        jsonstr,
        classes={MyClass: lambda dct: MyClass(dct['x'])},
        cls=ClassJSONDecoder
    )
    assert obj == obj2