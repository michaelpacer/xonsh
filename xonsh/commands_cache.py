# -*- coding: utf-8 -*-
"""Module for caching command & alias names as well as for predicting whether
a command will be able to be run in the background.

A background predictor is a function that accepect a single argument list
and returns whethere or not the process can be run in the background (returns
True) or must be run the foreground (returns False).
"""
import os
import builtins
import argparse
import collections
import collections.abc as cabc

from xonsh.platform import ON_WINDOWS, pathbasename
from xonsh.tools import executables_in
from xonsh.lazyasd import lazyobject


class CommandsCache(cabc.Mapping):
    """A lazy cache representing the commands available on the file system.
    The keys are the command names and the values a tuple of (loc, has_alias)
    where loc is either a str pointing to the executable on the file system or
    None (if no executable exists) and has_alias is a boolean flag for whether
    the command has an alias.
    """

    def __init__(self):
        self._cmds_cache = {}
        self._path_checksum = None
        self._alias_checksum = None
        self._path_mtime = -1
        self.backgroundable_predictors = default_backgroundable_predictors()

    def __contains__(self, key):
        _ = self.all_commands
        return self.lazyin(key)

    def __iter__(self):
        for cmd, (path, is_alias) in self.all_commands.items():
            if ON_WINDOWS and not is_alias:
                # All comand keys are stored in uppercase on Windows.
                # This ensures the original command name is returned.
                cmd = os.path.basename(path)
            yield cmd

    def __len__(self):
        return len(self.all_commands)

    def __getitem__(self, key):
        _ = self.all_commands
        return self.lazyget(key)

    def is_empty(self):
        """Returns whether the cache is populated or not."""
        return len(self._cmds_cache) == 0

    @staticmethod
    def get_possible_names(name):
        """Generates the possible `PATHEXT` extension variants of a given executable
         name on Windows as a list, conserving the ordering in `PATHEXT`.
         Returns a list as `name` being the only item in it on other platforms."""
        if ON_WINDOWS:
            name = name.upper()
            return [
                name + ext
                for ext in ([''] + builtins.__xonsh_env__['PATHEXT'])
            ]
        else:
            return [name]

    @property
    def all_commands(self):
        paths = builtins.__xonsh_env__.get('PATH', [])
        pathset = frozenset(x for x in paths if os.path.isdir(x))
        # did PATH change?
        path_hash = hash(pathset)
        cache_valid = path_hash == self._path_checksum
        self._path_checksum = path_hash
        # did aliases change?
        alss = getattr(builtins, 'aliases', set())
        al_hash = hash(frozenset(alss))
        cache_valid = cache_valid and al_hash == self._alias_checksum
        self._alias_checksum = al_hash
        # did the contents of any directory in PATH change?
        max_mtime = 0
        for path in pathset:
            mtime = os.stat(path).st_mtime
            if mtime > max_mtime:
                max_mtime = mtime
        cache_valid = cache_valid and (max_mtime <= self._path_mtime)
        self._path_mtime = max_mtime
        if cache_valid:
            return self._cmds_cache
        allcmds = {}
        for path in reversed(paths):
            # iterate backwards so that entries at the front of PATH overwrite
            # entries at the back.
            for cmd in executables_in(path):
                key = cmd.upper() if ON_WINDOWS else cmd
                allcmds[key] = (os.path.join(path, cmd), cmd in alss)
        only_alias = (None, True)
        for cmd in alss:
            if cmd not in allcmds:
                key = cmd.upper() if ON_WINDOWS else cmd
                allcmds[key] = only_alias
        self._cmds_cache = allcmds
        return allcmds

    def cached_name(self, name):
        """Returns the name that would appear in the cache, if it was exists."""
        if name is None:
            return None
        cached = pathbasename(name)
        if ON_WINDOWS:
            keys = self.get_possible_names(cached)
            cached = next((k for k in keys if k in self._cmds_cache), None)
        return cached

    def lazyin(self, key):
        """Checks if the value is in the current cache without the potential to
        update the cache. It just says whether the value is known *now*. This
        may not reflect precisely what is on the $PATH.
        """
        return self.cached_name(key) in self._cmds_cache

    def lazyiter(self):
        """Returns an iterator over the current cache contents without the
        potential to update the cache. This may not reflect what is on the
        $PATH.
        """
        return iter(self._cmds_cache)

    def lazylen(self):
        """Returns the length of the current cache contents without the
        potential to update the cache. This may not reflect precisely
        what is on the $PATH.
        """
        return len(self._cmds_cache)

    def lazyget(self, key, default=None):
        """A lazy value getter."""
        return self._cmds_cache.get(self.cached_name(key), default)

    def locate_binary(self, name):
        """Locates an executable on the file system using the cache."""
        # make sure the cache is up to date by accessing the property
        _ = self.all_commands
        return self.lazy_locate_binary(name)

    def lazy_locate_binary(self, name):
        """Locates an executable in the cache, without checking its validity."""
        possibilities = self.get_possible_names(name)
        if ON_WINDOWS:
            # Windows users expect to be able to execute files in the same
            # directory without `./`
            local_bin = next((fn for fn in possibilities if os.path.isfile(fn)),
                             None)
            if local_bin:
                return os.path.abspath(local_bin)
        cached = next((cmd for cmd in possibilities if cmd in self._cmds_cache),
                      None)
        if cached:
            return self._cmds_cache[cached][0]
        elif os.path.isfile(name) and name != os.path.basename(name):
            return name

    def predict_backgroundable(self, cmd):
        """Predics whether a command list is backgroundable."""
        name = self.cached_name(cmd[0])
        path, is_alias = self.lazyget(name, (None, None))
        if path is None or is_alias:
            return True
        predictor = self.backgroundable_predictors[name]
        return predictor(cmd[1:])

#
# Background Predictors
#

def predict_true(args):
    """Always say the process is backgroundable."""
    return True


def predict_false(args):
    """Never say the process is backgroundable."""
    return False


@lazyobject
def SHELL_PREDICTOR_PARSER():
    p = argparse.ArgumentParser('shell')
    p.add_argument('-c', nargs='?', default=None)
    p.add_argument('filename', nargs='?', default=None)
    return p


def predict_shell(args):
    """Precict the backgroundability of the normal shell interface, which
    comes down to whether it is being run in subproc mode.
    """
    ns, _ = SHELL_PREDICTOR_PARSER.parse_known_args(args)
    if ns.c is None and ns.filename is None:
        pred = False
    else:
        pred = True
    return pred


def default_backgroundable_predictors():
    """Generates a new defaultdict for known backgroundable predictors.
    The default is to predict true.
    """
    return collections.defaultdict(lambda: predict_true,
        sh=predict_shell,
        zsh=predict_shell,
        ksh=predict_shell,
        csh=predict_shell,
        tcsh=predict_shell,
        bash=predict_shell,
        fish=predict_shell,
        xonsh=predict_shell,
        ssh=predict_false,
        startx=predict_false,
        vi=predict_false,
        )
