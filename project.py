# Copyright (C) 2008 The Android Open Source Project
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import filecmp
import os
import re
import shutil
import stat
import sys
import urllib2

from color import Coloring
from git_command import GitCommand
from git_config import GitConfig, IsId
from gerrit_upload import UploadBundle
from error import GitError, ImportError, UploadError
from remote import Remote
from codereview import proto_client

HEAD    = 'HEAD'
R_HEADS = 'refs/heads/'
R_TAGS  = 'refs/tags/'
R_PUB   = 'refs/published/'
R_M     = 'refs/remotes/m/'

def _warn(fmt, *args):
  msg = fmt % args
  print >>sys.stderr, 'warn: %s' % msg

def _info(fmt, *args):
  msg = fmt % args
  print >>sys.stderr, 'info: %s' % msg

def not_rev(r):
  return '^' + r

class DownloadedChange(object):
  _commit_cache = None

  def __init__(self, project, base, change_id, ps_id, commit):
    self.project = project
    self.base = base
    self.change_id = change_id
    self.ps_id = ps_id
    self.commit = commit

  @property
  def commits(self):
    if self._commit_cache is None:
      self._commit_cache = self.project.bare_git.rev_list(
        '--abbrev=8',
        '--abbrev-commit',
        '--pretty=oneline',
        '--reverse',
        '--date-order',
        not_rev(self.base),
        self.commit,
        '--')
    return self._commit_cache


class ReviewableBranch(object):
  _commit_cache = None

  def __init__(self, project, branch, base):
    self.project = project
    self.branch = branch
    self.base = base

  @property
  def name(self):
    return self.branch.name

  @property
  def commits(self):
    if self._commit_cache is None:
      self._commit_cache = self.project.bare_git.rev_list(
        '--abbrev=8',
        '--abbrev-commit',
        '--pretty=oneline',
        '--reverse',
        '--date-order',
        not_rev(self.base),
        R_HEADS + self.name,
        '--')
    return self._commit_cache

  @property
  def date(self):
    return self.project.bare_git.log(
      '--pretty=format:%cd',
      '-n', '1',
      R_HEADS + self.name,
      '--')

  def UploadForReview(self):
    self.project.UploadForReview(self.name)

  @property
  def tip_url(self):
    me = self.project.GetBranch(self.name)
    commit = self.project.bare_git.rev_parse(R_HEADS + self.name)
    return 'http://%s/r/%s' % (me.remote.review, commit[0:12])

  @property
  def owner_email(self):
    return self.project.UserEmail


class StatusColoring(Coloring):
  def __init__(self, config):
    Coloring.__init__(self, config, 'status')
    self.project   = self.printer('header',    attr = 'bold')
    self.branch    = self.printer('header',    attr = 'bold')
    self.nobranch  = self.printer('nobranch',  fg = 'red')

    self.added     = self.printer('added',     fg = 'green')
    self.changed   = self.printer('changed',   fg = 'red')
    self.untracked = self.printer('untracked', fg = 'red')


class DiffColoring(Coloring):
  def __init__(self, config):
    Coloring.__init__(self, config, 'diff')
    self.project   = self.printer('header',    attr = 'bold')


class _CopyFile:
  def __init__(self, src, dest):
    self.src = src
    self.dest = dest

  def _Copy(self):
    src = self.src
    dest = self.dest
    # copy file if it does not exist or is out of date
    if not os.path.exists(dest) or not filecmp.cmp(src, dest):
      try:
        # remove existing file first, since it might be read-only
        if os.path.exists(dest):
          os.remove(dest)
        shutil.copy(src, dest)
        # make the file read-only
        mode = os.stat(dest)[stat.ST_MODE]
        mode = mode & ~(stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH)
        os.chmod(dest, mode)
      except IOError:
           print >>sys.stderr, \
              'error: Cannot copy file %s to %s' \
              % (src, dest)


class Project(object):
  def __init__(self,
               manifest,
               name,
               remote,
               gitdir,
               worktree,
               relpath,
               revision):
    self.manifest = manifest
    self.name = name
    self.remote = remote
    self.gitdir = gitdir
    self.worktree = worktree
    self.relpath = relpath
    self.revision = revision
    self.snapshots = {}
    self.extraRemotes = {}
    self.copyfiles = []
    self.config = GitConfig.ForRepository(
                    gitdir = self.gitdir,
                    defaults =  self.manifest.globalConfig)

    self.work_git = self._GitGetByExec(self, bare=False)
    self.bare_git = self._GitGetByExec(self, bare=True)

  @property
  def Exists(self):
    return os.path.isdir(self.gitdir)

  @property
  def CurrentBranch(self):
    """Obtain the name of the currently checked out branch.
       The branch name omits the 'refs/heads/' prefix.
       None is returned if the project is on a detached HEAD.
    """
    try:
      b = self.work_git.GetHead()
    except GitError:
      return None
    if b.startswith(R_HEADS):
      return b[len(R_HEADS):]
    return None

  def IsDirty(self, consider_untracked=True):
    """Is the working directory modified in some way?
    """
    self.work_git.update_index('-q',
                               '--unmerged',
                               '--ignore-missing',
                               '--refresh')
    if self.work_git.DiffZ('diff-index','-M','--cached',HEAD):
      return True
    if self.work_git.DiffZ('diff-files'):
      return True
    if consider_untracked and self.work_git.LsOthers():
      return True
    return False

  _userident_name = None
  _userident_email = None

  @property
  def UserName(self):
    """Obtain the user's personal name.
    """
    if self._userident_name is None:
      self._LoadUserIdentity()
    return self._userident_name

  @property
  def UserEmail(self):
    """Obtain the user's email address.  This is very likely
       to be their Gerrit login.
    """
    if self._userident_email is None:
      self._LoadUserIdentity()
    return self._userident_email

  def _LoadUserIdentity(self):
      u = self.bare_git.var('GIT_COMMITTER_IDENT')
      m = re.compile("^(.*) <([^>]*)> ").match(u)
      if m:
        self._userident_name = m.group(1)
        self._userident_email = m.group(2)
      else:
        self._userident_name = ''
        self._userident_email = ''

  def GetRemote(self, name):
    """Get the configuration for a single remote.
    """
    return self.config.GetRemote(name)

  def GetBranch(self, name):
    """Get the configuration for a single branch.
    """
    return self.config.GetBranch(name)


## Status Display ##

  def PrintWorkTreeStatus(self):
    """Prints the status of the repository to stdout.
    """
    if not os.path.isdir(self.worktree):
      print ''
      print 'project %s/' % self.relpath
      print '  missing (run "repo sync")'
      return

    self.work_git.update_index('-q',
                               '--unmerged',
                               '--ignore-missing',
                               '--refresh')
    di = self.work_git.DiffZ('diff-index', '-M', '--cached', HEAD)
    df = self.work_git.DiffZ('diff-files')
    do = self.work_git.LsOthers()
    if not di and not df and not do:
      return

    out = StatusColoring(self.config)
    out.project('project %-40s', self.relpath + '/')

    branch = self.CurrentBranch
    if branch is None:
      out.nobranch('(*** NO BRANCH ***)')
    else:
      out.branch('branch %s', branch)
    out.nl()

    paths = list()
    paths.extend(di.keys())
    paths.extend(df.keys())
    paths.extend(do)

    paths = list(set(paths))
    paths.sort()

    for p in paths:
      try: i = di[p]
      except KeyError: i = None

      try: f = df[p]
      except KeyError: f = None
 
      if i: i_status = i.status.upper()
      else: i_status = '-'

      if f: f_status = f.status.lower()
      else: f_status = '-'

      if i and i.src_path:
        line = ' %s%s\t%s => (%s%%)' % (i_status, f_status,
                                        i.src_path, p, i.level)
      else:
        line = ' %s%s\t%s' % (i_status, f_status, p)

      if i and not f:
        out.added('%s', line)
      elif (i and f) or (not i and f):
        out.changed('%s', line)
      elif not i and not f:
        out.untracked('%s', line)
      else:
        out.write('%s', line)
      out.nl()

  def PrintWorkTreeDiff(self):
    """Prints the status of the repository to stdout.
    """
    out = DiffColoring(self.config)
    cmd = ['diff']
    if out.is_on:
      cmd.append('--color')
    cmd.append(HEAD)
    cmd.append('--')
    p = GitCommand(self,
                   cmd,
                   capture_stdout = True,
                   capture_stderr = True)
    has_diff = False
    for line in p.process.stdout:
      if not has_diff:
        out.nl()
        out.project('project %s/' % self.relpath)
        out.nl()
        has_diff = True
      print line[:-1]
    p.Wait()


## Publish / Upload ##

  def WasPublished(self, branch):
    """Was the branch published (uploaded) for code review?
       If so, returns the SHA-1 hash of the last published
       state for the branch.
    """
    try:
      return self.bare_git.rev_parse(R_PUB + branch)
    except GitError:
      return None

  def CleanPublishedCache(self):
    """Prunes any stale published refs.
    """
    heads = set()
    canrm = {}
    for name, id in self._allrefs.iteritems():
      if name.startswith(R_HEADS):
        heads.add(name)
      elif name.startswith(R_PUB):
        canrm[name] = id

    for name, id in canrm.iteritems():
      n = name[len(R_PUB):]
      if R_HEADS + n not in heads:
        self.bare_git.DeleteRef(name, id)

  def GetUploadableBranches(self):
    """List any branches which can be uploaded for review.
    """
    heads = {}
    pubed = {}

    for name, id in self._allrefs.iteritems():
      if name.startswith(R_HEADS):
        heads[name[len(R_HEADS):]] = id
      elif name.startswith(R_PUB):
        pubed[name[len(R_PUB):]] = id

    ready = []
    for branch, id in heads.iteritems():
      if branch in pubed and pubed[branch] == id:
        continue

      branch = self.GetBranch(branch)
      base = branch.LocalMerge
      if branch.LocalMerge:
        rb = ReviewableBranch(self, branch, base)
        if rb.commits:
          ready.append(rb)
    return ready

  def UploadForReview(self, branch=None):
    """Uploads the named branch for code review.
    """
    if branch is None:
      branch = self.CurrentBranch
    if branch is None:
      raise GitError('not currently on a branch')

    branch = self.GetBranch(branch)
    if not branch.LocalMerge:
      raise GitError('branch %s does not track a remote' % branch.name)
    if not branch.remote.review:
      raise GitError('remote %s has no review url' % branch.remote.name)

    dest_branch = branch.merge
    if not dest_branch.startswith(R_HEADS):
      dest_branch = R_HEADS + dest_branch

    base_list = []
    for name, id in self._allrefs.iteritems():
      if branch.remote.WritesTo(name):
        base_list.append(not_rev(name))
    if not base_list:
      raise GitError('no base refs, cannot upload %s' % branch.name)

    print >>sys.stderr, ''
    _info("Uploading %s to %s:", branch.name, self.name)
    try:
      UploadBundle(project = self,
                   server = branch.remote.review,
                   email = self.UserEmail,
                   dest_project = self.name,
                   dest_branch = dest_branch,
                   src_branch = R_HEADS + branch.name,
                   bases = base_list)
    except proto_client.ClientLoginError:
      raise UploadError('Login failure')
    except urllib2.HTTPError, e:
      raise UploadError('HTTP error %d' % e.code)

    msg = "posted to %s for %s" % (branch.remote.review, dest_branch)
    self.bare_git.UpdateRef(R_PUB + branch.name,
                            R_HEADS + branch.name,
                            message = msg)


## Sync ##

  def Sync_NetworkHalf(self):
    """Perform only the network IO portion of the sync process.
       Local working directory/branch state is not affected.
    """
    if not self.Exists:
      print >>sys.stderr
      print >>sys.stderr, 'Initializing project %s ...' % self.name
      self._InitGitDir()
    self._InitRemote()
    for r in self.extraRemotes.values():
      if not self._RemoteFetch(r.name):
        return False
    if not self._RemoteFetch():
      return False
    self._RepairAndroidImportErrors()
    self._InitMRef()
    return True
    
  def _CopyFiles(self):
    for file in self.copyfiles:
      file._Copy()

  def _RepairAndroidImportErrors(self):
    if self.name in ['platform/external/iptables',
                     'platform/external/libpcap',
                     'platform/external/tcpdump',
                     'platform/external/webkit',
                     'platform/system/wlan/ti']:
      # I hate myself for doing this...
      #
      # In the initial Android 1.0 release these projects were
      # shipped, some users got them, and then the history had
      # to be rewritten to correct problems with their imports.
      # The 'android-1.0' tag may still be pointing at the old
      # history, so we need to drop the tag and fetch it again.
      #
      try:
        remote = self.GetRemote(self.remote.name)
        relname = remote.ToLocal(R_HEADS + 'release-1.0')
        tagname = R_TAGS + 'android-1.0'
        if self._revlist(not_rev(relname), tagname):
          cmd = ['fetch', remote.name, '+%s:%s' % (tagname, tagname)]
          GitCommand(self, cmd, bare = True).Wait()
      except GitError:
        pass

  def Sync_LocalHalf(self):
    """Perform only the local IO portion of the sync process.
       Network access is not required.

       Return:
         True:  the sync was successful
         False: the sync requires user input
    """
    self._InitWorkTree()
    self.CleanPublishedCache()

    rem = self.GetRemote(self.remote.name)
    rev = rem.ToLocal(self.revision)
    branch = self.CurrentBranch

    if branch is None:
      # Currently on a detached HEAD.  The user is assumed to
      # not have any local modifications worth worrying about.
      #
      lost = self._revlist(not_rev(rev), HEAD)
      if lost:
        _info("[%s] Discarding %d commits", self.name, len(lost))
      try:
        self._Checkout(rev, quiet=True)
      except GitError:
        return False
      self._CopyFiles()
      return True

    branch = self.GetBranch(branch)
    merge = branch.LocalMerge

    if not merge:
      # The current branch has no tracking configuration.
      # Jump off it to a deatched HEAD.
      #
      _info("[%s] Leaving %s"
            " (does not track any upstream)",
            self.name,
            branch.name)
      try:
        self._Checkout(rev, quiet=True)
      except GitError:
        return False
      self._CopyFiles()
      return True

    upstream_gain = self._revlist(not_rev(HEAD), rev)
    pub = self.WasPublished(branch.name)
    if pub:
      not_merged = self._revlist(not_rev(rev), pub)
      if not_merged:
        if upstream_gain:
          # The user has published this branch and some of those
          # commits are not yet merged upstream.  We do not want
          # to rewrite the published commits so we punt.
          #
          _info("[%s] Branch %s is published,"
                " but is now %d commits behind.",
                self.name, branch.name, len(upstream_gain))
          _info("[%s] Consider merging or rebasing the"
                " unpublished commits.", self.name)
        return True

    if merge == rev:
      try:
        old_merge = self.bare_git.rev_parse('%s@{1}' % merge)
      except GitError:
        old_merge = merge
      if old_merge == '0000000000000000000000000000000000000000' \
         or old_merge == '':
        old_merge = merge
    else:
      # The upstream switched on us.  Time to cross our fingers
      # and pray that the old upstream also wasn't in the habit
      # of rebasing itself.
      #
      _info("[%s] Manifest switched from %s to %s",
            self.name, merge, rev)
      old_merge = merge

    if rev == old_merge:
      upstream_lost = []
    else:
      upstream_lost = self._revlist(not_rev(rev), old_merge)

    if not upstream_lost and not upstream_gain:
      # Trivially no changes caused by the upstream.
      #
      return True

    if self.IsDirty(consider_untracked=False):
      _warn('[%s] commit (or discard) uncommitted changes'
            ' before sync', self.name)
      return False

    if upstream_lost:
      # Upstream rebased.  Not everything in HEAD
      # may have been caused by the user.
      #
      _info("[%s] Discarding %d commits removed from upstream",
            self.name, len(upstream_lost))

    branch.remote = rem
    branch.merge = self.revision
    branch.Save()

    my_changes = self._revlist(not_rev(old_merge), HEAD)
    if my_changes:
      try:
        self._Rebase(upstream = old_merge, onto = rev)
      except GitError:
        return False
    elif upstream_lost:
      try:
        self._ResetHard(rev)
      except GitError:
        return False
    else:
      try:
        self._FastForward(rev)
      except GitError:
        return False

    self._CopyFiles()
    return True

  def AddCopyFile(self, src, dest):
    # dest should already be an absolute path, but src is project relative
    # make src an absolute path
    src = os.path.join(self.worktree, src)
    self.copyfiles.append(_CopyFile(src, dest))

  def DownloadPatchSet(self, change_id, patch_id):
    """Download a single patch set of a single change to FETCH_HEAD.
    """
    remote = self.GetRemote(self.remote.name)

    cmd = ['fetch', remote.name]
    cmd.append('refs/changes/%2.2d/%d/%d' \
               % (change_id % 100, change_id, patch_id))
    cmd.extend(map(lambda x: str(x), remote.fetch))
    if GitCommand(self, cmd, bare=True).Wait() != 0:
      return None
    return DownloadedChange(self,
                            remote.ToLocal(self.revision),
                            change_id,
                            patch_id,
                            self.bare_git.rev_parse('FETCH_HEAD'))


## Branch Management ##

  def StartBranch(self, name):
    """Create a new branch off the manifest's revision.
    """
    branch = self.GetBranch(name)
    branch.remote = self.GetRemote(self.remote.name)
    branch.merge = self.revision

    rev = branch.LocalMerge
    cmd = ['checkout', '-b', branch.name, rev]
    if GitCommand(self, cmd).Wait() == 0:
      branch.Save()
    else:
      raise GitError('%s checkout %s ' % (self.name, rev))

  def PruneHeads(self):
    """Prune any topic branches already merged into upstream.
    """
    cb = self.CurrentBranch
    kill = []
    for name in self._allrefs.keys():
      if name.startswith(R_HEADS):
        name = name[len(R_HEADS):]
        if cb is None or name != cb:
          kill.append(name)

    rev = self.GetRemote(self.remote.name).ToLocal(self.revision)
    if cb is not None \
       and not self._revlist(HEAD + '...' + rev) \
       and not self.IsDirty(consider_untracked = False):
      self.work_git.DetachHead(HEAD)
      kill.append(cb)

    deleted = set()
    if kill:
      try:
        old = self.bare_git.GetHead()
      except GitError:
        old = 'refs/heads/please_never_use_this_as_a_branch_name'

      rm_re = re.compile(r"^Deleted branch (.*)\.$")
      try:
        self.bare_git.DetachHead(rev)

        b = ['branch', '-d']
        b.extend(kill)
        b = GitCommand(self, b, bare=True,
                       capture_stdout=True,
                       capture_stderr=True)
        b.Wait()
      finally:
        self.bare_git.SetHead(old)

      for line in b.stdout.split("\n"):
        m = rm_re.match(line)
        if m:
          deleted.add(m.group(1))

      if deleted:
        self.CleanPublishedCache()

    if cb and cb not in kill:
      kill.append(cb)
      kill.sort()

    kept = []
    for branch in kill:
      if branch not in deleted:
        branch = self.GetBranch(branch)
        base = branch.LocalMerge
        if not base:
          base = rev
        kept.append(ReviewableBranch(self, branch, base))
    return kept


## Direct Git Commands ##

  def _RemoteFetch(self, name=None):
    if not name:
      name = self.remote.name
    return GitCommand(self,
                      ['fetch', name],
                      bare = True).Wait() == 0

  def _Checkout(self, rev, quiet=False):
    cmd = ['checkout']
    if quiet:
      cmd.append('-q')
    cmd.append(rev)
    cmd.append('--')
    if GitCommand(self, cmd).Wait() != 0:
      if self._allrefs:
        raise GitError('%s checkout %s ' % (self.name, rev))

  def _ResetHard(self, rev, quiet=True):
    cmd = ['reset', '--hard']
    if quiet:
      cmd.append('-q')
    cmd.append(rev)
    if GitCommand(self, cmd).Wait() != 0:
      raise GitError('%s reset --hard %s ' % (self.name, rev))

  def _Rebase(self, upstream, onto = None):
    cmd = ['rebase', '-i']
    if onto is not None:
      cmd.extend(['--onto', onto])
    cmd.append(upstream)
    if GitCommand(self, cmd, disable_editor=True).Wait() != 0:
      raise GitError('%s rebase %s ' % (self.name, upstream))

  def _FastForward(self, head):
    cmd = ['merge', head]
    if GitCommand(self, cmd).Wait() != 0:
      raise GitError('%s merge %s ' % (self.name, head))

  def _InitGitDir(self):
    if not os.path.exists(self.gitdir):
      os.makedirs(self.gitdir)
      self.bare_git.init()
      self.config.SetString('core.bare', None)

      hooks = self._gitdir_path('hooks')
      try:
        to_rm = os.listdir(hooks)
      except OSError:
        to_rm = []
      for old_hook in to_rm:
        os.remove(os.path.join(hooks, old_hook))

      # TODO(sop) install custom repo hooks

      m = self.manifest.manifestProject.config
      for key in ['user.name', 'user.email']:
        if m.Has(key, include_defaults = False):
          self.config.SetString(key, m.GetString(key))

  def _InitRemote(self):
    if self.remote.fetchUrl:
      remote = self.GetRemote(self.remote.name)

      url = self.remote.fetchUrl
      while url.endswith('/'):
        url = url[:-1]
      url += '/%s.git' % self.name
      remote.url = url
      remote.review = self.remote.reviewUrl

      remote.ResetFetch()
      remote.Save()

    for r in self.extraRemotes.values():
      remote = self.GetRemote(r.name)
      remote.url = r.fetchUrl
      remote.review = r.reviewUrl
      remote.ResetFetch()
      remote.Save()

  def _InitMRef(self):
    if self.manifest.branch:
      msg = 'manifest set to %s' % self.revision
      ref = R_M + self.manifest.branch

      if IsId(self.revision):
        dst = self.revision + '^0',
        self.bare_git.UpdateRef(ref, dst, message = msg, detach = True)
      else:
        remote = self.GetRemote(self.remote.name)
        dst = remote.ToLocal(self.revision)
        self.bare_git.symbolic_ref('-m', msg, ref, dst)

  def _InitWorkTree(self):
    dotgit = os.path.join(self.worktree, '.git')
    if not os.path.exists(dotgit):
      os.makedirs(dotgit)

      topdir = os.path.commonprefix([self.gitdir, dotgit])
      if topdir.endswith('/'):
        topdir = topdir[:-1]
      else:
        topdir = os.path.dirname(topdir)

      tmpdir = dotgit
      relgit = ''
      while topdir != tmpdir:
        relgit += '../'
        tmpdir = os.path.dirname(tmpdir)
      relgit += self.gitdir[len(topdir) + 1:]

      for name in ['config',
                   'description',
                   'hooks',
                   'info',
                   'logs',
                   'objects',
                   'packed-refs',
                   'refs',
                   'rr-cache',
                   'svn']:
        os.symlink(os.path.join(relgit, name),
                   os.path.join(dotgit, name))

      rev = self.GetRemote(self.remote.name).ToLocal(self.revision)
      rev = self.bare_git.rev_parse('%s^0' % rev)

      f = open(os.path.join(dotgit, HEAD), 'wb')
      f.write("%s\n" % rev)
      f.close()

      cmd = ['read-tree', '--reset', '-u']
      cmd.append('-v')
      cmd.append('HEAD')
      if GitCommand(self, cmd).Wait() != 0:
        raise GitError("cannot initialize work tree")

  def _gitdir_path(self, path):
    return os.path.join(self.gitdir, path)

  def _revlist(self, *args):
    cmd = []
    cmd.extend(args)
    cmd.append('--')
    return self.work_git.rev_list(*args)

  @property
  def _allrefs(self):
    return self.bare_git.ListRefs()

  class _GitGetByExec(object):
    def __init__(self, project, bare):
      self._project = project
      self._bare = bare

    def ListRefs(self, *args):
      cmdv = ['for-each-ref', '--format=%(objectname) %(refname)']
      cmdv.extend(args)
      p = GitCommand(self._project,
                     cmdv,
                     bare = self._bare,
                     capture_stdout = True,
                     capture_stderr = True)
      r = {}
      for line in p.process.stdout:
        id, name = line[:-1].split(' ', 2)
        r[name] = id
      if p.Wait() != 0:
        raise GitError('%s for-each-ref %s: %s' % (
                       self._project.name,
                       str(args),
                       p.stderr))
      return r

    def LsOthers(self):
      p = GitCommand(self._project,
                     ['ls-files',
                      '-z',
                      '--others',
                      '--exclude-standard'],
                     bare = False,
                     capture_stdout = True,
                     capture_stderr = True)
      if p.Wait() == 0:
        out = p.stdout
        if out:
          return out[:-1].split("\0")
      return []

    def DiffZ(self, name, *args):
      cmd = [name]
      cmd.append('-z')
      cmd.extend(args)
      p = GitCommand(self._project,
                     cmd,
                     bare = False,
                     capture_stdout = True,
                     capture_stderr = True)
      try:
        out = p.process.stdout.read()
        r = {}
        if out:
          out = iter(out[:-1].split('\0'))
          while out:
            try:
              info = out.next()
              path = out.next()
            except StopIteration:
              break

            class _Info(object):
              def __init__(self, path, omode, nmode, oid, nid, state):
                self.path = path
                self.src_path = None
                self.old_mode = omode
                self.new_mode = nmode
                self.old_id = oid
                self.new_id = nid

                if len(state) == 1:
                  self.status = state
                  self.level = None
                else:
                  self.status = state[:1]
                  self.level = state[1:]
                  while self.level.startswith('0'):
                    self.level = self.level[1:]

            info = info[1:].split(' ')
            info =_Info(path, *info)
            if info.status in ('R', 'C'):
              info.src_path = info.path
              info.path = out.next()
            r[info.path] = info
        return r
      finally:
        p.Wait()

    def GetHead(self):
      return self.symbolic_ref(HEAD)

    def SetHead(self, ref, message=None):
      cmdv = []
      if message is not None:
        cmdv.extend(['-m', message])
      cmdv.append(HEAD)
      cmdv.append(ref)
      self.symbolic_ref(*cmdv)

    def DetachHead(self, new, message=None):
      cmdv = ['--no-deref']
      if message is not None:
        cmdv.extend(['-m', message])
      cmdv.append(HEAD)
      cmdv.append(new)
      self.update_ref(*cmdv)

    def UpdateRef(self, name, new, old=None,
                  message=None,
                  detach=False):
      cmdv = []
      if message is not None:
        cmdv.extend(['-m', message])
      if detach:
        cmdv.append('--no-deref')
      cmdv.append(name)
      cmdv.append(new)
      if old is not None:
        cmdv.append(old)
      self.update_ref(*cmdv)

    def DeleteRef(self, name, old=None):
      if not old:
        old = self.rev_parse(name)
      self.update_ref('-d', name, old)

    def rev_list(self, *args):
      cmdv = ['rev-list']
      cmdv.extend(args)
      p = GitCommand(self._project,
                     cmdv,
                     bare = self._bare,
                     capture_stdout = True,
                     capture_stderr = True)
      r = []
      for line in p.process.stdout:
        r.append(line[:-1])
      if p.Wait() != 0:
        raise GitError('%s rev-list %s: %s' % (
                       self._project.name,
                       str(args),
                       p.stderr))
      return r

    def __getattr__(self, name):
      name = name.replace('_', '-')
      def runner(*args):
        cmdv = [name]
        cmdv.extend(args)
        p = GitCommand(self._project,
                       cmdv,
                       bare = self._bare,
                       capture_stdout = True,
                       capture_stderr = True)
        if p.Wait() != 0:
          raise GitError('%s %s: %s' % (
                         self._project.name,
                         name,
                         p.stderr))
        r = p.stdout
        if r.endswith('\n') and r.index('\n') == len(r) - 1:
          return r[:-1]
        return r
      return runner


class MetaProject(Project):
  """A special project housed under .repo.
  """
  def __init__(self, manifest, name, gitdir, worktree):
    repodir = manifest.repodir
    Project.__init__(self,
                     manifest = manifest,
                     name = name,
                     gitdir = gitdir,
                     worktree = worktree,
                     remote = Remote('origin'),
                     relpath = '.repo/%s' % name,
                     revision = 'refs/heads/master')

  def PreSync(self):
    if self.Exists:
      cb = self.CurrentBranch
      if cb:
        base = self.GetBranch(cb).merge
        if base:
          self.revision = base

  @property
  def HasChanges(self):
    """Has the remote received new commits not yet checked out?
    """
    rev = self.GetRemote(self.remote.name).ToLocal(self.revision)
    if self._revlist(not_rev(HEAD), rev):
      return True
    return False