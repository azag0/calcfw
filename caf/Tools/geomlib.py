# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at http://mozilla.org/MPL/2.0/.
from itertools import chain, product, repeat
import os
from io import StringIO
import pkg_resources
import csv
from collections import OrderedDict

from typing import (
    List, Tuple, DefaultDict, Iterator, IO, Sized, Iterable, Union, Dict, Any,
    TYPE_CHECKING, TypeVar, Type
)

if TYPE_CHECKING:
    import numpy as np  # type: ignore
else:
    np = None  # lazy-loaded in Molecule constructor

specie_data = OrderedDict(
    (r['symbol'], {**r, 'number': int(r['number'])})  # type: ignore
    for r in csv.DictReader((
        l.decode() for l in pkg_resources.resource_stream(__name__, 'atom-data.csv')
    ), quoting=csv.QUOTE_NONNUMERIC)
)
bohr = 0.52917721092

Vec = Tuple[float, float, float]
_M = TypeVar('_M', bound='Molecule')


_string_cache: Dict[Any, str] = {}


class Atom:
    def __init__(self, specie: str, coord: Vec, ghost: bool = False) -> None:
        self.specie = specie
        self.coord: Vec = tuple(coord)  # type: ignore
        self.ghost = ghost

    @property
    def mass(self) -> float:
        mass: float = specie_data[self.specie]['mass']
        return mass

    @property
    def number(self) -> int:
        return int(specie_data[self.specie]['number'])

    @property
    def covalent_radius(self) -> float:
        r: float = specie_data[self.specie]['covalent radius']
        return r

    def copy(self) -> 'Atom':
        return Atom(self.specie, self.coord, self.ghost)


class Molecule(Sized, Iterable[Atom]):
    def __init__(self, atoms: List[Atom]) -> None:
        global np
        if np is None:
            import numpy as np
        self._atoms = atoms

    @classmethod
    def from_coords(cls: Type[_M], species: List[str], coords: List[Vec]
                    ) -> _M:
        return cls([Atom(sp, coord) for sp, coord in zip(species, coords)])

    @property
    def species(self) -> List[str]:
        return [atom.specie for atom in self]

    @property
    def numbers(self) -> List[int]:
        return [atom.number for atom in self]

    @property
    def mass(self) -> float:
        return sum(atom.mass for atom in self)

    @property
    def cms(self) -> 'np.ndarray':
        masses = np.array([atom.mass for atom in self])
        return (masses[:, None]*self.xyz).sum(0)/self.mass

    @property
    def inertia(self) -> 'np.ndarray':
        masses = np.array([atom.mass for atom in self])
        coords_w = np.sqrt(masses)[:, None]*(self.xyz-self.cms)
        A = np.array([np.diag(np.full(3, r)) for r in np.sum(coords_w**2, 1)])
        B = coords_w[:, :, None]*coords_w[:, None, :]
        return np.sum(A-B, 0)

    def __getitem__(self, i: int) -> Atom:
        return self._atoms[i]

    @property
    def coords(self) -> List[Vec]:
        return [atom.coord for atom in self]

    def __repr__(self) -> str:
        return "<{} '{}'>".format(self.__class__.__name__, self.formula)

    @property
    def xyz(self) -> 'np.ndarray':
        return np.array(self.coords)

    @property
    def formula(self) -> str:
        counter = DefaultDict[str, int](int)
        for specie in self.species:
            counter[specie] += 1
        return ''.join(
            f'{sp}{n if n > 1 else ""}' for sp, n in sorted(counter.items())
        )

    def bondmatrix(self, scale: float) -> 'np.ndarray':
        xyz = self.xyz
        Rs = np.array([atom.covalent_radius for atom in self])
        dmatrix = np.sqrt(np.sum((xyz[None, :]-xyz[:, None])**2, 2))
        thrmatrix = scale*(Rs[None, :]+Rs[:, None])
        return dmatrix < thrmatrix

    def get_fragments(self, scale: float = 1.3) -> List['Molecule']:
        bond = self.bondmatrix(scale)
        ifragments = getfragments(bond)
        fragments = [
            Molecule([self._atoms[i].copy() for i in fragment])
            for fragment in ifragments
        ]
        return fragments

    def hash(self) -> int:
        if len(self) == 1:
            return self[0].number
        return hash(tuple(np.round(sorted(np.linalg.eigvalsh(self.inertia)), 3)))

    def shifted(self: _M, delta: Union[Vec, 'np.ndarray']) -> _M:
        m = self.copy()
        for atom in m:
            c = atom.coord
            atom.coord = (c[0]+delta[0], c[1]+delta[1], c[2]+delta[2])
        return m

    def __add__(self: _M, other: object) -> _M:
        if not isinstance(other, Molecule):
            return NotImplemented
        geom = self.copy()
        geom._atoms.extend(other.copy())
        return geom

    def centered(self: _M) -> _M:
        return self.shifted(-self.cms)

    @property
    def centers(self) -> Iterator[Atom]:
        yield from self._atoms

    def __iter__(self) -> Iterator[Atom]:
        yield from (atom for atom in self._atoms if not atom.ghost)

    def __len__(self) -> int:
        return len([atom for atom in self._atoms if not atom.ghost])

    def __format__(self, fmt: str) -> str:
        fp = StringIO()
        self.dump(fp, fmt)
        return fp.getvalue()

    def items(self) -> Iterator[Tuple[str, Vec]]:
        for atom in self:
            yield atom.specie, atom.coord

    dumps = __format__

    def dump(self, f: IO[str], fmt: str) -> None:
        if fmt == '':
            f.write(repr(self))
        elif fmt == 'xyz':
            f.write('{}\n'.format(len(self)))
            f.write('Formula: {}\n'.format(self.formula))
            for specie, coord in self.items():
                f.write('{:>2} {}\n'.format(
                    specie, ' '.join('{:15.8}'.format(x) for x in coord)
                ))
        elif fmt == 'aims':
            for atom in self.centers:
                specie, r = atom.specie, atom.coord
                key = (specie, r, atom.ghost, fmt)
                try:
                    f.write(_string_cache[key])
                except KeyError:
                    kind = 'atom' if not atom.ghost else 'empty'
                    s = f'{kind} {r[0]:15.8f} {r[1]:15.8f} {r[2]:15.8f} {specie:>2}\n'
                    f.write(s)
                    _string_cache[key] = s
        elif fmt == 'mopac':
            f.write('* Formula: {}\n'.format(self.formula))
            for specie, coord in self.items():
                f.write('{:>2} {}\n'.format(
                    specie, ' '.join('{:15.8} 1'.format(x) for x in coord)
                ))
        else:
            raise ValueError("Unknown format: '{}'".format(fmt))

    def copy(self: _M) -> _M:
        return type(self)([atom.copy() for atom in self._atoms])

    def ghost(self: _M) -> _M:
        m = self.copy()
        for atom in m:
            atom.ghost = True
        return m

    def write(self, filename: str) -> None:
        ext = os.path.splitext(filename)[1]
        if ext == '.xyz':
            fmt = 'xyz'
        elif ext == '.xyzc':
            fmt = 'xyzc'
        elif ext == '.aims' or os.path.basename(filename) == 'geometry.in':
            fmt = 'aims'
        elif ext == '.mopac':
            fmt = 'mopac'
        with open(filename, 'w') as f:
            self.dump(f, fmt)


class Crystal(Molecule):
    def __init__(self, atoms: List[Atom], lattice: List[Vec]) -> None:
        super().__init__(atoms)
        self.lattice = lattice

    @classmethod
    def from_coords(cls, species: List[str], coords: List[Vec],  # type: ignore
                    lattice: List[Vec]) -> 'Crystal':
        return cls(
            [Atom(sp, coord) for sp, coord in zip(species, coords)],
            lattice
        )

    def dump(self, f: IO[str], fmt: str) -> None:
        if fmt == '':
            f.write(repr(self))
        elif fmt == 'aims':
            for label, (x, y, z) in zip('abc', self.lattice):
                f.write(f'lattice_vector {x:15.8f} {y:15.8f} {z:15.8f}\n')
            super().dump(f, fmt)
        elif fmt == 'vasp':
            f.write(f'Formula: {self.formula}\n')
            f.write(f'{1:15.8f}\n')
            for x, y, z in self.lattice:
                f.write(f'{x:15.8f} {y:15.8f} {z:15.8f}\n')
            species: Dict[str, List[Atom]] = OrderedDict((sp, []) for sp in set(self.species))
            f.write(' '.join(species.keys()) + '\n')
            for atom in self:
                species[atom.specie].append(atom)
            f.write(' '.join(str(len(atoms)) for atoms in species.values()) + '\n')
            f.write('cartesian\n')
            for atom in chain(*species.values()):
                r = atom.coord
                s = f'{r[0]:15.8f} {r[1]:15.8f} {r[2]:15.8f}\n'
                f.write(s)
        else:
            raise ValueError(f'Unknown format: {fmt!r}')

    def copy(self) -> 'Crystal':
        return Crystal(
            [atom.copy() for atom in self._atoms],
            self.lattice.copy()
        )

    @property
    def abc(self) -> 'np.ndarray':
        return np.array(self.lattice)

    def get_kgrid(self, density: float = 0.06) -> Tuple[int, int, int]:
        rec_lattice = 2*np.pi*np.linalg.inv(self.abc.T)
        rec_lens = np.sqrt((rec_lattice**2).sum(1))
        nkpts = np.ceil(rec_lens/(density*bohr))
        return int(nkpts[0]), int(nkpts[1]), int(nkpts[2])

    def supercell(self, ns: Tuple[int, int, int]) -> 'Crystal':
        abc = self.abc
        latt_vectors = np.array([
            sum(s*vec for s, vec in zip(shift, abc))
            for shift in product(*map(range, ns))
        ])
        species = list(chain.from_iterable(repeat(self.species, len(latt_vectors))))
        coords = [
            (x, y, z) for x, y, z in
            (self.xyz[None, :, :]+latt_vectors[:, None, :]).reshape((-1, 3))
        ]
        lattice = [(x, y, z) for x, y, z in abc*np.array(ns)[:, None]]
        return Crystal.from_coords(species, coords, lattice)

    def normalized(self) -> 'Crystal':
        xyz = np.mod(self.xyz@np.linalg.inv(self.lattice), 1)@self.lattice
        return Crystal.from_coords(self.species, xyz, self.lattice.copy())


def get_vec(ws: List[str]) -> Vec:
    return float(ws[0]), float(ws[1]), float(ws[2])


def load(fp: IO[str], fmt: str) -> Molecule:
    if fmt == 'xyz':
        n = int(fp.readline())
        fp.readline()
        species = []
        coords = []
        for _ in range(n):
            ws = fp.readline().split()
            species.append(ws[0])
            coords.append(get_vec(ws[1:4]))
        return Molecule.from_coords(species, coords)
    elif fmt == 'xyzc':
        n = int(fp.readline())
        lattice = []
        for _ in range(3):
            lattice.append(get_vec(fp.readline().split()))
        species = []
        coords = []
        for _ in range(n):
            ws = fp.readline().split()
            species.append(ws[0])
            coords.append(get_vec(ws[1:4]))
        return Crystal.from_coords(species, coords, lattice)
    if fmt == 'aims':
        atoms = []
        lattice = []
        while True:
            line = fp.readline()
            if line == '':
                break
            line = line.strip()
            if not line or line.startswith('#'):
                continue
            ws = line.split()
            what = ws[0]
            if what in ['atom', 'empty']:
                atoms.append(Atom(ws[4], get_vec(ws[1:4]), ghost=what == 'empty'))
            elif what == 'lattice_vector':
                lattice.append(get_vec(ws[1:4]))
        if lattice:
            assert len(lattice) == 3
            return Crystal(atoms, lattice)
        else:
            return Molecule(atoms)
    raise ValueError(f'Unknown format: {fmt}')


def loads(s: str, fmt: str) -> Molecule:
    fp = StringIO(s)
    return load(fp, fmt)


def readfile(path: str, fmt: str = None) -> Molecule:
    if not fmt:
        ext = os.path.splitext(path)[1]
        if ext == '.xyz':
            fmt = 'xyz'
        elif ext == '.aims' or os.path.basename(path) == 'geometry.in':
            fmt = 'aims'
        elif ext == '.xyzc':
            fmt = 'xyzc'
        else:
            raise RuntimeError('Cannot determine format')
    with open(path) as f:
        return load(f, fmt)


def getfragments(C: 'np.ndarray') -> List[List[int]]:
    """Find fragments within a set of sparsely connected elements.

    Given square matrix C where C_ij = 1 if i and j are connected
    and 0 otherwise, it extends the connectedness (if i and j and j and k
    are connected, i and k are also connected) and returns a list sets of
    elements which are not connected by any element.

    The algorithm visits all elements, checks whether it wasn't already
    assigned to a fragment, if not, it crawls it's neighbors and their
    neighbors etc., until it cannot find any more neighbors. Then it goes
    to the next element until all were visited.
    """
    n = C.shape[0]
    assigned = [-1 for _ in range(n)]  # fragment index, otherwise -1
    ifragment = 0  # current fragment index
    queue = [0 for _ in range(n)]  # allocate queue of neighbors
    for elem in range(n):  # iterate over elements
        if assigned[elem] >= 0:  # skip if assigned
            continue
        queue[0], a, b = elem, 0, 1  # queue starting with the element itself
        while b-a > 0:  # until queue is exhausted
            node, a = queue[a], a+1  # pop from queue
            assigned[node] = ifragment  # assign node
            neighbors = np.flatnonzero(C[node, :])  # list of neighbors
            for neighbor in neighbors:
                if not (assigned[neighbor] >= 0 or neighbor in queue[a:b]):
                    # add to queue if not assigned or in queue
                    queue[b], b = neighbor, b+1
        ifragment += 1
    fragments = [[i for i, f in enumerate(assigned) if f == fragment]
                 for fragment in range(ifragment)]
    return fragments
