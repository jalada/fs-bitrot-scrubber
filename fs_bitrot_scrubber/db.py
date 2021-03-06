#-*- coding: utf-8 -*-

import itertools as it, operator as op, functools as ft
from contextlib import contextmanager, closing
from datetime import datetime
from time import time
from os.path import exists
import os, sys, sqlite3, logging, hashlib

from fs_bitrot_scrubber.fadvise import fadvise
from fs_bitrot_scrubber import force_unicode


class FileNode(object):

	src_fadvise_count, src_fadvise_bs = 0, 60 * 2**20 # 60 MiB

	def __init__(self, query_func, log, src, row, checksum, use_fadvise=True):
		self.q, self.log, self.meta, self.src = query_func, log, row, src
		self.log.debug(force_unicode('Checking file: {}'.format(row['path'])))
		self.src_meta, self.src_checksum = self.stat(), checksum()

		self.src_fadvise = bool(use_fadvise)
		if use_fadvise\
				and use_fadvise is not True\
				and isinstance(use_fadvise, (int, long)):
			self.src_fadvise_bs = use_fadvise
		self.fadvise(seq=True)

	def fadvise(self, read_bytes=None, **fadvise_kwz):
		'Advise kernel to avoid caching read data in RAM.'
		if not self.src_fadvise: return
		if read_bytes is None:
			return fadvise(self.src, **fadvise_kwz)
		assert not fadvise_kwz, fadvise_kwz
		self.src_fadvise_count += read_bytes
		if self.src_fadvise_count > self.src_fadvise_bs:
			fadvise(self.src, drop_cache=True)
			self.src_fadvise_count = 0

	def stat(self):
		# ctime change is also important here,
		#  as it may indicate changes with reverted mtime,
		#  which will produce false-positive otherwise
		return op.attrgetter('st_size', 'st_ctime', 'st_mtime')(os.fstat(self.src.fileno()))

	def read(self, bs=2 * 2**20):
		chunk = self.src.read(bs)
		if self.stat() != self.src_meta:
			# Bail out if file changes while it's being hashed
			self.q( 'UPDATE files SET dirty = 1,'
				' last_skip = ? WHERE path = ?', (time(), self.meta['path']) )
			return 0
		if chunk:
			self.src_checksum.update(chunk)
			self.fadvise(len(chunk))
		else:
			digest = self.src_checksum.digest()
			size, ctime, mtime = self.src_meta
			if self.meta['checksum'] != digest: # either new hash or changes
				if self.meta['checksum'] is not None: # can still be intentional change w/ reverted mtime
					if max(abs(self.meta['ctime'] - ctime), abs(self.meta['mtime'] - mtime)) >= 1:
						self.log.info(force_unicode( 'Detected change in'
							' file contents and ctime: {}'.format(self.meta['path']) ))
					else: # bitrot!!!
						self.log.error(force_unicode( 'Detected'
							' unmarked changes: {}'.format(self.meta['path']) ))
			# Update with last-seen metadata,
			#  regardless of what was set in metadata_check()
			self.q( 'UPDATE files SET dirty = 0, clean = 1,'
					' size = ?, mtime = ?, ctime = ?, checksum = ?, last_scrub = ?,'
					' last_skip = NULL WHERE path = ?',
				(size, mtime, ctime, digest, time(), self.meta['path']) )
		return len(chunk)

	def close(self):
		if self.src_fadvise: self.fadvise(drop_cache=True)
		self.src.close()
		self.src = self.src_meta = self.src_checksum = None


class MetaDB(object):

	# clean - file was checked in this generation
	# dirty - mtime/size was updated in this generation
	# checksum - hash (binary)
	# last_scrub - last time "clean" was set to true
	# last_skip - last time failed to checksum due to rapid changes
	_db_init = '''
		CREATE TABLE IF NOT EXISTS files (
			path BLOB PRIMARY KEY ON CONFLICT REPLACE NOT NULL,
			generation INT NOT NULL,
			size INT NOT NULL,
			mtime REAL NOT NULL,
			ctime REAL NOT NULL,
			clean BOOLEAN NOT NULL,
			dirty BOOLEAN NOT NULL,
			checksum BLOB NULL,
			last_scrub REAL NULL,
			last_skip REAL NULL
		);
		CREATE INDEX IF NOT EXISTS files_checksum
			ON files (generation, checksum, last_skip, last_scrub);
		CREATE INDEX IF NOT EXISTS files_clean
			ON files (generation, clean, last_skip, last_scrub);
		CREATE INDEX IF NOT EXISTS files_dirty
			ON files (generation, dirty, last_skip, last_scrub);
		CREATE INDEX IF NOT EXISTS files_gen
			ON files (generation);

		CREATE TABLE IF NOT EXISTS meta (
			var TEXT PRIMARY KEY ON CONFLICT REPLACE NOT NULL,
			val TEXT NOT NULL
		);
	'''

	_db_migrations = []

	_db = None


	def __init__( self, path, path_check=None, checksum=None,
			use_fadvise=True, log=None, log_queries=False, commit_after=None ):
		self._log = logging.getLogger('bitrot_scrubber.MetaDB') if not log else log
		self._log_sql = log_queries
		self._checksum = hashlib.sha256 if not checksum else checksum
		self._use_fadvise = use_fadvise
		self._db_path, self._db_parity = path, path_check

		# commit_after should be a tuple of (queries, seconds)
		seq, ts = (None, None) if commit_after else\
			((v if v and v>=0 else None) for v in commit_after)
		self._db_seq_limit, self._db_ts_limit = seq, ts
		self._db_seq, self._db_ts = 0, time()

		self._init_db()

	@contextmanager
	def _cursor(self, query, params=tuple(), **kwz):
		if self._log_sql:
			self._log.debug(force_unicode('Query: {!r}, data: {!r}'.format(query, params)))
		try:
			with closing(self._db.execute(query, params, **kwz)) as c: yield c
		finally:
			self._db_seq, ts = self._db_seq + 1, time()
			if (self._db_ts_limit and (ts - self._db_ts) >= self._db_ts_limit)\
					or (self._db_seq_limit and self._db_seq >= self._db_seq_limit):
				self._db.commit()
				self._db_seq = 0
			self._db_ts = ts

	def _query(self, *query_argz, **query_kwz):
		with self._cursor(*query_argz, **query_kwz): pass

	def _parity_check(self):
		# TODO: use zfec or something similar here
		if not self._db_parity or not exists(self._db_parity): return
		digest = hashlib.sha256()
		with open(self._db_path) as db:
			for chunk in iter(ft.partial(db.read, 2**20), ''): digest.update(chunk)
		assert open(self._db_parity).read().strip() == digest.hexdigest(), 'DB check failed'

	def _parity_write(self):
		# TODO: use zfec or something similar here
		if not self._db_parity: return
		digest = hashlib.sha256()
		with open(self._db_path) as db:
			for chunk in iter(ft.partial(db.read, 2**20), ''): digest.update(chunk)
		open(self._db_parity, 'w').write(digest.hexdigest())

	def _init_db(self):
		self._parity_check()
		self._db = sqlite3.connect(self._db_path)
		self._db.row_factory, self._db.text_factory = sqlite3.Row, str
		with self._db as db: db.executescript(self._db_init)
		with self._cursor("SELECT val FROM meta WHERE var = 'schema_version' LIMIT 1") as c:
			row = c.fetchone()
			schema_ver = int(row['val']) if row else 1
		for schema_ver, query in enumerate(
			self._db_migrations[schema_ver-1:], schema_ver ): db.executescript(query)
		self._query( 'INSERT INTO meta (var, val)'
			" VALUES ('schema_version', '{}')".format(schema_ver + 1) )

	def close(self):
		if self._db:
			self._db.commit()
			self._db.close()
			self._db = None
			if exists(self._db_path):
				self._parity_write()

	def __enter__(self): return self
	def __exit__(self, *err): self.close()
	def __del__(self): self.close()


	def get_generation(self, new=True):
		with self._cursor('SELECT generation'
				' FROM files ORDER BY generation DESC LIMIT 1') as c:
			row = c.fetchone()
		gen = row['generation'] if row else 0
		if new: gen += 1
		return gen

	def set_generation(self, new=True):
		self.generation = self.get_generation(new=new)


	def metadata_check(self, path, size, mtime, ctime):
		with self._cursor('SELECT * FROM files WHERE path = ? LIMIT 1', (path,)) as c:
			row = c.fetchone()
		if not row:
			self._query( 'INSERT INTO files (path, generation,'
					' size, mtime, ctime, clean, dirty) VALUES (?, ?, ?, ?, ?, 0, 0)',
				(path, self.generation, size, mtime, ctime) )
			return True
		dirty = row['dirty']
		if not dirty and not (abs(row['mtime'] - mtime) <= 1 and row['size'] == size): dirty = True
		else: ctime = row['ctime'] # so it won't be set to a new value
		self._query( 'UPDATE files SET generation = ?, ctime = ?,'
			' clean = 0, dirty = ? WHERE path = ?', (self.generation, ctime, dirty, path) )
		return dirty

	def metadata_clean(self):
		self._query('DELETE FROM files WHERE generation < ?', (self.generation,))

	def get_file_to_scrub(self, skip_for=3 * 3600, skip_until=0):
		while True:
			query_base = 'SELECT * FROM files WHERE generation = ?'\
				' AND (last_skip IS NULL OR last_skip < ?) {} ORDER BY last_scrub LIMIT 1'
			query_params = [self.generation, skip_until]
			# First try to hash not-yet-seen files
			with self._cursor(query_base.format('AND checksum IS NULL'), query_params) as c:
				row = c.fetchone()
			if not row:
				# Then dirty (changed) files
				with self._cursor(query_base.format('AND dirty = 1'), query_params) as c:
					row = c.fetchone()
			if not row:
				# Then just not-yet-checked for this generation
				with self._cursor(query_base.format('AND clean = 0'), query_params) as c:
					row = c.fetchone()
			if not row and skip_until == 0:
				# Then try to find a path that was skipped a while ("skip_for") ago
				skip_until = time() - skip_for
				if skip_until != 0: return self.get_file_to_scrub(skip_until=skip_until)
			if not row: return # nothing more/yet to check
			try: src = open(row['path'])
			except (IOError, OSError):
				self._log.debug(force_unicode( 'Failed to open'
					' scanned path, skipping it: {}'.format(row['path']) ))
				self.drop_file(row['path'])
				continue
			return FileNode( self._query, self._log, src, row,
				checksum=self._checksum, use_fadvise=self._use_fadvise )

	def drop_file(self, path):
		self._query('DELETE FROM files WHERE generation = ? AND path = ?', (self.generation, path))

	def list_paths(self):
		with self._cursor('SELECT * FROM files') as c:
			for row in c:
				yield dict(
					path=row['path'], clean=bool(row['clean']), dirty=bool(row['dirty']),
					last_scrub=datetime.fromtimestamp(row['last_scrub']) if row['last_scrub'] else None,
					last_skip=datetime.fromtimestamp(row['last_skip']) if row['last_skip'] else None )
