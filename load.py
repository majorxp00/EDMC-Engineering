# EDMC plugin — sends the journal events this tool actually reads, as they happen.
#
# What it does, and nothing else: watches for the events (the WANTED set below) the server's journal reader
# consumes, batches them, and POSTs them to /api/v1/ingest/events with the account's plugin key.
# It sends no other event. Besides the game's journal and its NavRoute.json it reads only its own queue file and EDMC's
# settings.
#
# Three things it is careful about, because the server is careful about them:
#
#   Replays are safe.   The site remembers which events it applied (each carries `_occ`, its number among
#                       identical events of one second) and skips only those, so a batch resent after a
#                       failed send is counted once, while an older event it never had is still applied.
#                       That means the safe move on any failure is to keep the batch and retry.
#   The key is judged.  Present a key and you are judged on the key: a revoked one is refused even
#                       from a signed-in browser. Only a 401 or 403 carrying the site's own
#                       refusal code (key-invalid, key-not-allowed, sign-in) pauses sending and says so
#                       (a block page with the same status is retried); so does a redirect, which
#                       blames the site address. The plugin keeps running and resumes as soon as a new
#                       key or site is saved.
#   Order matters.      Events are applied oldest-first by timestamp, so the queue stays in the
#                       order the game wrote them and is never reordered here. A newer event of a
#                       commander is never sent before an older one that still waits, except an event
#                       the site refused: it is set aside (S.parked) and retried, so it blocks nobody.
#   Nothing is dropped on a guess. Every failure (an error, a refusal, a reply that stored nothing)
#                       keeps the batch and backs off. An event is dropped only when: the site
#                       refused it again 24 hours or more after it first refused it (retried after 10
#                       minutes, 30 minutes, 1 hour, then every 2 hours; a site that does not offer
#                       retries is handled as before: refused events of a mixed reply are dropped);
#                       a retried event comes back "too-old" (the site can no longer check it);
#                       the site named it as from a beta or legacy session; it cannot be encoded (not
#                       valid JSON, or over EVENT_MAX_BYTES); or the queue is past MAX_QUEUE (see
#                       _trim_pending). Each of these sets the drop note on the EDMC line.
#
# The queue (with the parked events) is kept in a file beside the plugin, rewritten within SAVE_DELAY seconds
# of a new event, after failed sends, when it empties, every 10th batch while draining, and when EDMC closes normally.
# A write that fails while EDMC runs shows on the EDMC line and is retried; one that fails as EDMC
# closes is only logged, and the events it held are lost unless the startup read of the newest journal
# finds them again. If EDMC is killed, at most the last ~SAVE_DELAY seconds of events are lost, with the
# same exception. Up to 5,000 waiting events are kept (past that the least useful are dropped, see
# _trim_pending).

import bisect
import json
import logging
import os
import queue
import re
import threading
import time
import tkinter as tk
from tkinter import ttk
from urllib.parse import urljoin, urlsplit

import requests

try:                                    # EDMC's own config; absent when run outside it.
    from config import appname, config
except ImportError:                     # pragma: no cover - only taken by the offline test
    appname, config = 'EDMarketConnector', None

try:                                    # EDMC's own user-agent string (what it sends to other sites)
    from config import user_agent
except ImportError:                     # pragma: no cover - older EDMC, or run outside it
    user_agent = appname

logger = logging.getLogger('%s.%s' % (appname, os.path.basename(os.path.dirname(__file__))))

PLUGIN_NAME = 'EDMC-Engineering'
DEFAULT_HOST = 'https://ed.golegend.com'   # pre-fills Site; a saved value always wins
VERSION = '0.6.8'

# The events the server's journal reader consumes. Anything else is not sent: a plugin that
# forwarded the whole journal would ship far more than the tool uses. Most are sent whole; Location,
# FSDJump, CarrierJump, Docked, Undocked, FSDTarget, MissionCompleted, LoadGame and Statistics are trimmed in _shape.
# Movement is sent only as far as
# the Roadmap uses it. The tool sends plotted routes (since 0.2.0) (NavRoute / NavRouteClear /
# FSDTarget) so a step can say "on your way: your route ends in <engineer's system>, N jumps left".
WANTED = frozenset((
    'Fileheader', 'Commander', 'LoadGame',
    'Rank', 'Progress', 'Reputation', 'Statistics',
    'Materials', 'MaterialCollected', 'MaterialDiscarded', 'MaterialTrade',
    'EngineerCraft', 'EngineerProgress',
    'Synthesis', 'TechnologyBroker', 'MissionCompleted', 'EngineerContribution', 'ScientificResearch',
    'Loadout', 'ShipyardSwap', 'StoredShips', 'ShipyardBuy', 'ShipyardSell', 'SellShipOnRebuy',
    'Location', 'FSDJump', 'CarrierJump', 'Docked', 'Undocked',
    'NavRoute', 'NavRouteClear', 'FSDTarget',
))

# Events not held for the batch timer: they change what the next screen says. Everything else waits
# for the batch timer, so a materials-gathering run is one request rather than two hundred.
# MaterialTrade is a single deliberate action at a trader and the player is watching for the result,
# so it goes as soon as its second has settled:
# SETTLE_SECONDS after the last line of that second, or at once when a later line arrives; the bulk
# MaterialCollected/Discarded floods of a mining run still batch.
URGENT = frozenset(('EngineerCraft', 'EngineerProgress', 'Loadout', 'ShipyardSwap', 'LoadGame',
                    'ShipyardBuy', 'ShipyardSell', 'SellShipOnRebuy',
                    'MaterialTrade',
                    # Where you are and where you are going: the Roadmap flips to "on your way" /
                    # "you're here" on these, so they go at once (a handful per jump, not a flood).
                    'Location', 'FSDJump', 'CarrierJump', 'Docked', 'Undocked',
                    'NavRoute', 'NavRouteClear', 'FSDTarget'))

# Every wake either sends the queued batch or, with nothing queued, polls for a destination the
# site may have sent (0.3.0). A destination clicked on the site shows within one wake plus the
# request plus the UI's own 1s tick, which is why the site says "within 15 seconds". That is the
# poll while the game runs or events wait; with the game closed and nothing queued it is every 2 minutes.
WAKE_SECONDS = 10
IDLE_WAKE_SECONDS = 120
MAX_BATCH = 200
SETTLE_SECONDS = 2      # the newest journal second waits this long after its last line (unless a later line
                        # comes first), so a second travels whole
QUEUE_VERSION = 1                       # format version written into the queue file
MAX_QUEUE = 5000                        # events kept waiting; past this the least useful are dropped
TRIM_TO = 4500                          # trimmed down to this, so the trim is not re-run on every event
URGENT_RETRY = 15                       # an urgent event pulls a longer back-off in to this, never closer
EVENT_MAX_BYTES = 512 * 1024            # one event over this (as JSON) cannot be sent; the site would refuse the body
DRAIN_GAP = 2                           # seconds between batches while draining a long queue
RETRY_AFTER_MAX = 3600                  # clamp for a Retry-After from the site
MOVES = frozenset(('Location', 'FSDJump', 'CarrierJump', 'Docked', 'Undocked',
                   'NavRoute', 'NavRouteClear', 'FSDTarget'))
# Location, FSDJump, CarrierJump and Docked are sent trimmed to the fields the site reads; Undocked to its
# time; FSDTarget to the target's name and the jumps left; LoadGame to the fields in LOADGAME_KEEP.
MOVE_KEEP = ('timestamp', 'event', 'StarSystem', 'StarPos', 'StationName', 'StationType', 'Docked',
             'StationServices', 'StationEconomies', 'MarketID')
UNDOCKED_KEEP = ('timestamp', 'event')
FSDTARGET_KEEP = ('timestamp', 'event', 'Name', 'RemainingJumpsInRoute')
GAME_QUIET_AFTER = 900                  # seconds with no journal line before the game counts as not running (a crash)
LOADGAME_KEEP = ('timestamp', 'event', 'Commander', 'FID', 'Credits', 'Horizons', 'Odyssey', 'GameMode',
                 'ShipID', 'Ship', 'Ship_Localised', 'gameversion')
# Statistics: only the counters engineers ask for (the site keeps the same list).
STATS_KEEP = {'Combat': ('Bounties_Claimed', 'Combat_Bonds'), 'Mining': ('Quantity_Mined',),
              'Smuggling': ('Black_Markets_Traded_With',), 'Trading': ('Markets_Traded_With',)}
DETAIL_WRAP = 360                       # pixels: the EDMC notes line wraps here instead of widening the window
SAVE_RETRY = 5                          # seconds after a failed queue write before the next try
SAVE_DELAY = 2                          # seconds from the first unsaved event to its write to queue.json
SEED_RETRY = 5                          # seconds between tries of a failed startup read
SEED_TRIES = 3                          # tries of the startup read before carrying on without it
BLOCK_RETRY = 60                        # seconds a commander the site answered 409 for is set aside
TIMEOUT = 20
SAVER_TICK = 0.5                        # how often the saver thread looks at the save deadline
BODY_MAX = 1024 * 1024                  # a reply body over this is not read further (real ones are a few hundred bytes)
BACKOFF = (5, 15, 60, 300)              # seconds, then stays at the last one
UNREACHABLE_AFTER = sum(BACKOFF[:-1])   # seconds of failing before the line says so (4th failure)
DROP_NOTICE_SECONDS = 600               # how long the main window shows a dropped-events note
NOT_LIVE_NOTE = '%d events from a beta or legacy game session were not stored'
REFUSAL_CODES = ('key-invalid', 'key-not-allowed', 'sign-in')   # the server's own 401/403 codes
PARK_WAIT = (600, 1800, 3600, 7200)     # seconds before a refused event is retried; the last repeats
PARK_GIVE_UP = 86400                    # a retried event refused again this long after its first refusal is dropped
HOLD_BEFORE_PARK = 3600                 # a batch the site stored none of is held this long, then probed
PARK_MAX = 1000                         # parked events kept; past this the oldest are dropped
DEST_MAX = 80                           # characters, system and station each — matches the server


class State:
    """Everything mutable, in one place, so the worker and the UI thread share one story."""

    def __init__(self):
        self.host = ''
        self.key = ''
        self.fid = None
        self.fid_gen = 0                # counts every assignment of fid by a live journal line
        self.pass_errors = 0            # worker only: sender passes in a row that raised
        self.save_due = 0.0             # time.monotonic() the queue must be written by; 0 = nothing due
        self.seed_fails = 0             # worker only: failed startup reads so far
        self.seed_requested = False     # the worker reads the journal on its next pass (never the UI thread)
        self.held_header = None         # a live Fileheader waiting for the commander line that follows it
        self.sent_with = None           # worker only: (host, key) the last _send used
        self.blocked = {}               # fid -> time.monotonic() of a 409; skipped until the retry time
        self.conflict = False           # worker only: the last _send got a 409
        self.held_back = set()          # worker only: fids the site said it could not place
        self.outbox = queue.Queue()
        self.game_seen = 0.0            # time.monotonic() of the last non-Shutdown journal line (UI thread writes)
        self.game_on = False            # any journal line seen and no Shutdown since (plain flag, UI thread writes)
        self.pending = []               # events waiting for the next send, oldest first
        self.lock = threading.Lock()
        self.stop = threading.Event()
        self.refused = False            # paused until a new key is saved: key refused OR site address wrong
        self.site_note = ''             # set when the pause is about the site address (redirect / not https)
        self.newest = ('', 0.0)         # (newest journal timestamp seen, monotonic time it was seen)
        self.settle_at = 0.0            # worker: when the newest second settles, if a batch is waiting on it
        self.not_before = 0.0           # worker: no request before this time.monotonic() value
        self.wake_floor = 0.0           # the earliest a new event may pull not_before in to
        self.batch_limit = MAX_BATCH    # halved while the site refuses a batch as malformed or too large
        self.probe = set()              # fids that got a 409: only their oldest event is sent until a 200
        self.dropped = 0                # events dropped because the queue was full
        self.retry_after = None         # seconds the site asked for on the last 429/503
        # the last _send got the site's own JSON 400/413 (the code) or a 200 that stored nothing ('could not apply')
        self.rejected = False
        self.server_error = False       # the last _send got the site's own JSON 500
        self.refused_now = 0            # worker only: events the last 200 named as refused while it stored others
        self.refused_names = []         # worker only: their event names
        self.refused_evs = []           # worker only: events the last 200 refused, to be taken again
        self.parked_now = False         # worker only: the last sent batch parked events
        self.whole_refusal = False      # worker only: the last _send was a 200 refusing all, none stored
        self.applied_n = 0              # worker only: events the last 200 stored
        self.hold_since = {}            # worker only: fid -> monotonic time of the first whole refusal of its streak
        self.suspects = set()           # worker only: id() of probe events held back while a witness is tried
        self.suspect_fid = None
        self.parked = []                # refused events set aside: {'ev', 'since', 'next', 'tries'}, wall-clock seconds
        self.retrying = {}              # id(event) -> its parked record, while the event is back in pending
        self.occ = ['', {}]             # UI thread only: (journal second, identical-event counts within it)
        self.thread = None
        self.queue_unread = False       # the saved queue file was locked at start; it is read again before each save
        self.pending_file = None        # this session's own pending file, replaced on each failed save
        self.carried = []               # pending files merged in; removed once a queue write holds them
        self.closed = False             # set by the clean-exit write; after it nothing writes the queue file
        self.tick_failed = False        # a _tick failure was already logged with its traceback
        self.status = 'not configured'
        self.detail = ''
        self.sent = 0
        self.queue_file = ''
        self.status_var = None
        self.detail_var = None
        self.detail_label = None
        self.down_since = None          # worker: monotonic time of the first failure since the site last answered
        self.trouble_since = None       # worker: first failure the site itself answered, since its last 2xx
        self.answered = False           # the last _send got an HTTP reply of any kind
        self.drop_parts = {}            # fresh drop notes by kind, merged into drop_note
        self.drop_note = ''             # why events were dropped; shown for DROP_NOTICE_SECONDS
        self.drop_at = 0.0
        self.skipped_old = set()        # id() of batch events the site answered 'too-old' (from skipped_events)
        self.host_var = None
        self.key_var = None
        self.frame = None
        self.ui_thread = None
        # Destinations (0.3.0): worker hands the UI a place to show and copy; the UI hands the
        # worker an ack to send back. Never crossed the other way.
        self.dest_in = queue.Queue()    # worker -> UI: destinations to show, drained on the UI thread
        self.dest_seen = ''             # worker only: the last destination id handed to the UI
        self.dest_ack = None            # {'id', 'copied'}, set by the UI, read/cleared by the worker, under self.lock
        self.dest_text = ''             # UI only: the line currently shown
        self.dest_var = None
        self.dest_label = None
        self.dest_button = None
        self.dest_shown = None          # UI only: the destination dict currently shown (for Copy)
        self.saver = None               # saves the queue while the worker is inside a send
        self.unsaved = False            # events queued since the last queue-file write
        self.saves_skipped = 0          # worker only: successful batches since the last queue save
        self.save_failed = False        # the last queue-file write failed; cleared by the next one that succeeds
        self.unsent_state = None        # latest _write_unsent: 'kept' (side file written), 'lost' (failed), None
        self.unsent_warned = False      # the pending-file write failure was already warned of in this streak


S = State()


# ---------- EDMC entry points ----------

def plugin_start3(plugin_dir):
    S.queue_file = os.path.join(plugin_dir, 'queue.json')
    _load_settings()
    _load_queue()
    if S.host and S.key:
        S.seed_requested = True
        S.seed_fails = 0
    with S.lock:
        _trim_pending()
    S.thread = threading.Thread(target=_worker, name='edeng-sender', daemon=True)
    S.thread.start()
    S.saver = threading.Thread(target=_saver, name='edeng-saver', daemon=True)
    S.saver.start()
    S.outbox.put('send')
    _set_status()
    return PLUGIN_NAME


def plugin_stop():
    S.stop.set()
    S.outbox.put(None)
    if S.thread:
        S.thread.join(timeout=5)
    if S.saver:
        S.saver.join(timeout=2)
    with S.lock:
        if S.held_header is not None:
            S.pending.append(dict(S.held_header, _fid=S.fid) if S.fid else S.held_header)
            S.held_header = None
    _save_queue(final=True)


def journal_entry(cmdr, is_beta, system, station, entry, state):
    """Called by EDMC for every journal line. Keep it fast: queue and return."""
    ts = entry.get('timestamp')
    if isinstance(ts, str) and ts >= S.newest[0]:
        S.newest = (ts, time.monotonic())
    if S.settle_at and S.host and S.key:
        S.outbox.put('send')    # a later line may have settled a second that a batch is waiting on
    if entry.get('event') in ('Shutdown', 'ShutDown'):
        S.game_on = False
    else:
        now = time.monotonic()
        was_on = _game_running(now)
        S.game_on, S.game_seen = True, now
        if not was_on and S.host and S.key:
            S.outbox.put('send')    # the game just started: poll at once
    if is_beta:
        return
    event = entry.get('event')
    if event not in WANTED:
        return
    # LoadGame carries the FID, which is how the server tells one commander from another on the
    # same account. Send it with every batch once we know it.
    if event in ('LoadGame', 'Commander') and entry.get('FID'):
        S.fid = entry['FID']
        S.fid_gen += 1
    # EDMC keeps the parsed NavRoute.json in state['NavRoute']; _shape builds the sent form.
    entry = _shape(entry, _route_from(entry, (state or {}).get('NavRoute')))
    if entry is None:
        return
    entry = dict(entry, _occ=_next_occ(S.occ, entry))
    if not S.fid:
        state_fid = (state or {}).get('FID')    # EDMC's state carries the FID (unverified, so guarded)
        if isinstance(state_fid, str) and state_fid:
            S.fid = state_fid
            S.fid_gen += 1
    # Stamp the commander this event belongs to NOW. A batch can hold events from two commanders
    # (relog while the site was unreachable), so the FID must travel with each event, not the batch.
    if S.fid:
        entry = dict(entry, _fid=S.fid)
    if not S.host or not S.key:
        return   # nothing to send it to: queuing would only grow without limit
    with S.lock:
        if event == 'Fileheader':
            # Written before the game knows the commander: hold it until the next event, so the
            # Commander line can give it the right FID instead of the previous session's.
            S.held_header = entry
            return
        if S.held_header is not None:
            head, S.held_header = S.held_header, None
            S.pending.append(dict(head, _fid=S.fid) if S.fid else head)
        S.pending.append(entry)
        S.unsaved = True
        armed = not S.save_due
        if armed:
            S.save_due = time.monotonic() + SAVE_DELAY
        _trim_pending()
        overflowing = len(S.pending) >= MAX_BATCH
    if overflowing or event in URGENT:
        S.outbox.put('send')
    if armed:
        S.outbox.put('save')    # the worker (or the saver thread during a send) writes the queue file, not this thread
    _set_status()


def plugin_prefs(parent, cmdr, is_beta):
    # ttk inside EDMC's themed notebook frame. myNotebook's own Label/Entry raise AttributeError on some
    # EDMC builds, which EDMC shows as a missing preferences tab.
    frame = nb_frame(parent)
    S.host_var = tk.StringVar(value=S.host)
    S.key_var = tk.StringVar(value=S.key)

    ttk.Label(frame, text='Where your commander data goes. The key is issued on the account page '
                          'of the site, and is shown once.', wraplength=420, justify=tk.LEFT).grid(
        row=0, column=0, columnspan=2, sticky=tk.W, pady=(0, 8))
    ttk.Label(frame, text='Site').grid(row=1, column=0, sticky=tk.W)
    ttk.Entry(frame, textvariable=S.host_var, width=44).grid(row=1, column=1, sticky=tk.W, padx=6)
    ttk.Label(frame, text='Plugin key').grid(row=2, column=0, sticky=tk.W)
    ttk.Entry(frame, textvariable=S.key_var, width=44, show='•').grid(row=2, column=1, sticky=tk.W, padx=6, pady=4)
    ttk.Label(frame, text='Upload a journal folder on the site once before this can send anything: '
                          'live events are applied on top of what is already stored, so there has to '
                          'be something to apply them to.', wraplength=420, justify=tk.LEFT).grid(
        row=3, column=0, columnspan=2, sticky=tk.W, pady=(8, 0))
    return frame


def prefs_changed(cmdr, is_beta):
    before = (S.host, S.key)
    S.host = _clean_host(S.host_var.get() if S.host_var else '') or DEFAULT_HOST
    typed = (S.key_var.get() if S.key_var else '').strip()
    if not typed:
        S.key = ''    # clearing the box stops sending
    elif _key_ok(typed):
        S.key = typed
    else:
        logger.info('the pasted plugin key is not a site key; the saved key is kept')
    if config:
        config.set('edeng_host', S.host)
        config.set('edeng_key', S.key)
        # Persist now, not only on a clean exit. EDMC writes its config on shutdown, so a crash or a
        # forced kill between entering the key and quitting loses it — which is exactly what a restart
        # did once. save() flushes immediately; guarded because not every EDMC build exposes it.
        try:
            config.save()
        except Exception:
            logger.debug('config.save() unavailable or failed', exc_info=True)
    # A new key (or site) gets a fresh try at once: clear the pause, no restart. The sender thread
    # never exits on a refusal, so it is still there to read this; status is repainted by _tick on
    # the UI thread (this runs on it too).
    S.refused = False
    S.not_before = 0.0
    S.blocked = {}
    if (S.host, S.key) != before and S.host and S.key:
        S.seed_requested = True
        S.seed_fails = 0   # nothing was queued while unconfigured: the worker seeds the session so far
    S.down_since = S.trouble_since = None
    S.status = 'checking the new key'
    S.detail = ''
    _set_status()
    S.outbox.put('send')


def plugin_app(parent):
    """The one line this plugin is allowed on EDMC's main window.

    Plain tk widgets, NOT ttk: EDMC themes its main window by walking the widget tree and
    restyling tk.Frame/tk.Label to the current theme. ttk widgets keep their own style and render
    as white boxes on the dark window — which is exactly what they did here."""
    S.ui_thread = threading.current_thread()   # first, so no guard passes before the widgets exist
    frame = tk.Frame(parent)
    S.status_var = tk.StringVar(value=S.status)
    S.detail_var = tk.StringVar(value='')
    tk.Label(frame, text='Engineering:').grid(row=0, column=0, sticky=tk.W)
    tk.Label(frame, textvariable=S.status_var).grid(row=0, column=1, sticky=tk.W, padx=4)
    S.detail_label = tk.Label(frame, textvariable=S.detail_var, wraplength=DETAIL_WRAP, justify=tk.LEFT)
    S.detail_label.grid(row=1, column=0, columnspan=2, sticky=tk.W)
    S.detail_label.grid_remove()   # one quiet line: the second row appears only with something to say
    # Destinations (0.3.0): hidden until the first one arrives, so EDMC's window gets no blank row.
    S.dest_var = tk.StringVar(value=S.dest_text)
    S.dest_label = tk.Label(frame, textvariable=S.dest_var)
    S.dest_label.grid(row=2, column=0, columnspan=2, sticky=tk.W)
    S.dest_button = tk.Button(frame, text='Copy', command=_copy_again)
    S.dest_button.grid(row=2, column=2, sticky=tk.W, padx=4)
    if not S.dest_text:
        S.dest_label.grid_remove()
        S.dest_button.grid_remove()
    # The sender runs on its own thread and must never touch these StringVars (Tkinter is single
    # threaded; a cross-thread .set() raises and, uncaught, kills the sender so the queue stops
    # draining). Repaint here on EDMC's UI thread instead, once a second, from plain state the
    # worker updates.
    S.frame = frame
    frame.after(1000, _tick)
    return frame


def _tick():
    """Main-thread repaint. EDMC fires this on its UI thread via Tk's after(), so it is the one
    place allowed to write the StringVars; it reschedules itself until the frame goes away."""
    try:
        _take_destination()
        _set_status()
        S.tick_failed = False
    except Exception:
        if not S.tick_failed:
            S.tick_failed = True
            logger.exception('status repaint failed')
        else:
            logger.debug('status repaint failed again', exc_info=True)
    finally:
        if S.frame is not None:
            try:
                S.frame.after(1000, _tick)
            except tk.TclError:
                pass   # frame destroyed on shutdown — stop rescheduling


# ---------- the sender ----------

def _cycle(fails):
    """One worker pass: send whatever is queued, or — with nothing queued — poll the site for a
    destination and carry any pending ack. Split out from _worker so a test can drive exactly one
    pass, with no thread and no timing."""
    if S.seed_requested and S.host and S.key:
        S.seed_requested = False
        if _seed_from_journal() is False:
            S.seed_fails += 1
            if S.seed_fails < SEED_TRIES:
                # Send nothing before the seed: a live event applied first makes the site skip every older one.
                S.seed_requested = True
                now = time.monotonic()
                S.not_before = S.wake_floor = now + SEED_RETRY
                return fails
            logger.warning('the startup read of the journal failed %d times; carrying on without it', S.seed_fails)
    if not S.host or not S.key or S.refused:
        return fails
    _unpark(time.time())
    with S.lock:
        # Capped, so a large startup seed (a whole session's pickups) goes in chunks the server
        # will accept rather than one oversized body. One commander per batch.
        batch = _pick_batch()
    if not batch and S.settle_at:
        now = time.monotonic()      # events wait for their second to settle: no check-in, wake when it does
        S.not_before, S.wake_floor = max(S.settle_at, now), now
        return fails
    if not batch:
        ok, note, fatal = _send([])
        _note_reach(ok, fatal)
        if ok:
            # A completed send's own note ('12 applied') must never be overwritten by a poll's
            # silence; a 409 ('waiting') also lasts until a batch send succeeds.
            if S.status not in ('sent', 'waiting'):
                S.status = 'connected'
                S.detail = ''
            return fails
        if fatal and S.sent_with != (S.host, S.key):
            S.outbox.put('send')    # refused under settings that were replaced meanwhile
            return fails
        if fatal:
            # A key that is refused will be refused again. Say so and pause; the thread stays alive
            # so a new key saved in Settings resumes sending at once (prefs_changed clears this).
            _refuse(note)
            return fails
        # Silent: a site that is down, or one not yet carrying this destination change, costs one
        # failed request every wake. It must never stall a real send behind a multi-minute back-off.
        # The one exception: a Retry-After the site itself sent is honoured, so we do not ask again in 10 s.
        if S.retry_after:
            S.not_before = S.wake_floor = time.monotonic() + S.retry_after
        return fails
    attempt = time.monotonic()
    fid = batch[0].get('_fid')
    probing = S.batch_limit == 1 and attempt - S.hold_since.get(fid, attempt) >= HOLD_BEFORE_PARK
    same = bool(S.suspects) and fid == S.suspect_fid
    ok, note, fatal = _send(batch)
    _note_reach(ok, fatal)
    if ok:
        S.batch_limit = MAX_BATCH
        if S.applied_n > 0:
            S.hold_since.pop(fid, None)
        with S.lock:
            # Only drop what was actually sent: anything that arrived mid-flight stays queued, and so
            # does any commander the site said it could not place yet.
            gone = {id(e) for e in batch if e.get('_fid') not in S.held_back}
            S.pending = [e for e in S.pending if id(e) not in gone]
            S.sent += len(gone)
            _park_refused(batch, gone)
            if same and S.applied_n > 0:
                _park_suspects()    # the site stored this commander's others: the suspects were the bad ones
            more = bool(S.pending)
            for f in S.held_back:
                S.blocked[f] = time.monotonic()
                S.probe.add(f)
            if fid not in S.held_back:
                S.probe.discard(fid)
        fails = 0
        S.status = 'sent'
        S.detail = note
        if S.refused_now:
            # The site stored part of this batch and named the rest as refused. Stored events show its
            # write path works, so those named are the bad ones: dropped, and said so.
            _note_drop(key='refused', n=S.refused_now, names=[str(n) for n in S.refused_names] or ['unnamed'],
                       fmt=_REFUSED_FMT)
        # A long drain would rewrite the whole queue file every batch. Save when it empties or on
        # every 10th batch; a crash only means resending batches the site already has and skips.
        S.saves_skipped += 1
        if not more or S.saves_skipped >= 10 or S.refused_now or S.parked_now:
            S.saves_skipped = 0
            _save_queue()
        if more:
            S.not_before = S.wake_floor = time.monotonic() + DRAIN_GAP   # drain the rest, paced
    elif fatal and S.sent_with != (S.host, S.key):
        S.outbox.put('send')        # refused under settings that were replaced meanwhile
    elif fatal:
        _refuse(note)
    elif S.rejected in (400, 413) and S.batch_limit > 1 and len(batch) > 1:
        # The site's own JSON refusal of a body: halving costs nothing and may find the size it takes.
        S.batch_limit = max(1, min(S.batch_limit, len(batch)) // 2)
        S.not_before = S.wake_floor = time.monotonic() + DRAIN_GAP
        S.status = 'retrying'
        S.detail = '%s - sending smaller batches' % note
    elif S.whole_refusal and _held_whole(batch, fid, note, probing, same):
        pass
    elif S.rejected or S.server_error:
        # Still refused down to one event, a 200 that stored nothing, or the site's own 500: none of
        # these says the events are bad (the site never answers 400/500 about one event), so keep
        # everything, oldest first, and back off for as long as it lasts.
        fails = _hold(batch, note, fails, attempt)
    elif S.conflict:
        # The site has nothing stored for this commander yet. Set its events aside for a while and
        # let other commanders' events go; it is not a failure of the connection, so no back-off.
        with S.lock:
            S.blocked[fid] = time.monotonic()
            S.probe.add(fid)
            others = any(e.get('_fid') not in S.blocked for e in S.pending)
        S.status = 'waiting'
        S.detail = note
        if S.unsaved:
            _save_queue()
        if others:
            S.outbox.put('send')
    else:
        fails += 1
        S.status = 'retrying'
        S.detail = '%s — %d waiting' % (note, len(batch))
        _save_queue()
        _set_retry(fails, attempt)
    if same:
        S.suspects = set()
    _set_status()
    return fails


def _held_whole(batch, fid, note, probing, same):
    """Worker only. A whole refusal: the site stored none of the batch. True when it is dealt with here (a probe
    parked or its events made suspects); False to let _hold keep and back off as for any failure."""
    now = time.monotonic()
    if same:
        S.hold_since[fid] = now     # the witness was refused too: the fault is site-wide, suspects go back in front
        return False
    S.hold_since.setdefault(fid, now)
    if not probing:
        return False
    ids = {id(e) for e in batch}
    S.hold_since[fid] = now
    with S.lock:
        if any(e.get('_fid') == fid and id(e) not in ids for e in S.pending):
            S.suspects, S.suspect_fid = ids, fid
            S.batch_limit = MAX_BATCH
            S.not_before = S.wake_floor = now + DRAIN_GAP
            S.status, S.detail = 'retrying', '%s - holding %d events, nothing dropped' % (note, len(S.pending))
            return True
        S.pending = [e for e in S.pending if id(e) not in ids]
        _park_refused(batch, ids)       # nothing else waits: set aside as a mixed reply would
    S.batch_limit = MAX_BATCH
    S.status, S.detail = 'sent', note
    _save_queue()
    return True


def _park_suspects():
    """Caller holds S.lock. The site stored events beside the suspects, so they are the refused ones: parked."""
    sus = [e for e in S.pending if id(e) in S.suspects]
    gone = {id(e) for e in sus}
    S.pending = [e for e in S.pending if id(e) not in gone]
    S.refused_evs = sus
    _park_refused(sus, gone)


def _hold(batch, note, fails, attempt):
    """Worker only. Keep the batch, drop nothing. If another commander's events are waiting they go
    first and this one is set aside for BLOCK_RETRY; otherwise back off as for any failure."""
    fid = batch[0].get('_fid')
    if S.rejected == 'could not apply':
        # events written meanwhile must be able to join the batch; after an hour of whole refusals the next send probes
        long_hold = S.whole_refusal and time.monotonic() - S.hold_since.get(fid, time.monotonic()) >= HOLD_BEFORE_PARK
        S.batch_limit = 1 if long_hold else MAX_BATCH
    with S.lock:
        others = any(e.get('_fid') != fid and e.get('_fid') not in S.blocked for e in S.pending)
        held = len(S.pending)
    S.status = 'retrying'
    S.detail = '%s - holding %d events, nothing dropped' % (note, held)
    _save_queue()
    if others:
        S.blocked[fid] = time.monotonic()
        S.not_before = S.wake_floor = time.monotonic() + DRAIN_GAP
        return fails
    fails += 1
    _set_retry(fails, attempt)
    return fails


def _next_occ(counts, e):
    """`counts` is [second, {shape: n}]. The number of earlier identical events (leaving out `_` fields)
    in the same journal second, in delivery order: 0 for the first, 1 for its twin. Earlier seconds are forgotten."""
    ts = e.get('timestamp') if isinstance(e.get('timestamp'), str) else ''
    if counts[0] != ts:
        counts[0], counts[1] = ts, {}
    k = json.dumps({a: b for a, b in e.items() if not a.startswith('_')}, sort_keys=True)
    n = counts[1].get(k, 0)
    counts[1][k] = n + 1
    return n


def _unpark(now):
    """Worker only: refused events whose time has come go back into the queue, placed by timestamp."""
    with S.lock:
        due = [r for r in S.parked if r['next'] <= now]
        if not due:
            return
        S.parked = [r for r in S.parked if r['next'] > now]
        for r in sorted(due, key=lambda r: str(r['ev'].get('timestamp'))):
            ts = str(r['ev'].get('timestamp') or '')
            at = next((i for i, q in enumerate(S.pending) if str(q.get('timestamp') or '') > ts), len(S.pending))
            S.pending.insert(at, r['ev'])
            S.retrying[id(r['ev'])] = r
        S.unsaved = True
        if not S.save_due:
            S.save_due = time.monotonic() + SAVE_DELAY


def _park_refused(batch, gone):
    """Caller holds S.lock. After a sent batch: events the site refused (and will take again) are parked, or dropped
    with the note when they were already retried and refused again a day after the first refusal; retried events
    that were not refused are done."""
    refused = {id(e) for e in S.refused_evs}
    now = time.time()
    dropped = []
    for e in batch:
        if id(e) not in gone:
            continue
        rec = S.retrying.pop(id(e), None)
        if id(e) not in refused:
            if rec is not None and id(e) in S.skipped_old:
                dropped.append(e)   # the site can no longer check a retried event: said, not silent
            continue
        if rec is None:
            rec = {'ev': e, 'since': now, 'next': 0, 'tries': 0}
        else:
            rec['tries'] += 1
            if now - rec['since'] >= PARK_GIVE_UP:
                dropped.append(e)
                continue
        rec['next'] = now + PARK_WAIT[min(rec['tries'], len(PARK_WAIT) - 1)]
        S.parked.append(rec)
    S.parked_now = bool(refused)
    overflow = []
    while len(S.parked) > PARK_MAX:
        S.parked.sort(key=lambda r: r['since'])
        overflow.append(S.parked.pop(0)['ev'])
    if dropped:
        _note_drop(key='refused', n=len(dropped), names=sorted({str(e.get('event')) for e in dropped}),
                   fmt=_REFUSED_FMT)
    if overflow:
        _note_drop(key='overflow', n=len(overflow), names=sorted({str(e.get('event')) for e in overflow}),
                   fmt='the plugin dropped %d event(s) (%s) because too many were waiting for the site to store them')
    S.refused_evs = []
    S.skipped_old = set()


def _queue_snapshot():
    """Caller holds S.lock. (events, parked) as the queue file holds them: an event back in the queue for a retry is
    saved as a parked record, so that it keeps its count of tries."""
    here = {id(e) for e in S.pending}
    events = [e for e in S.pending if id(e) not in S.retrying]
    parked = [dict(r) for r in S.parked] + [dict(r) for i, r in S.retrying.items() if i in here]
    return events, parked


def _merge_parked(held):
    """Caller holds S.lock. Adds the parked records of a queue file that are not already held."""
    have = {(json.dumps(r['ev'], sort_keys=True), r['since']) for r in S.parked}
    for r in held.get('parked') or []:
        if not (isinstance(r, dict) and isinstance(r.get('ev'), dict) and r['ev'].get('event') in WANTED):
            continue
        since, nxt, tries = r.get('since'), r.get('next'), r.get('tries')
        if any(isinstance(v, bool) or not isinstance(v, (int, float)) for v in (since, nxt, tries)):
            continue
        k = (json.dumps(r['ev'], sort_keys=True), since)
        if k not in have:
            have.add(k)
            S.parked.append({'ev': r['ev'], 'since': since, 'next': nxt, 'tries': int(tries)})


_REFUSED_FMT = 'the site could not store %d event(s) (%s); they were dropped'


def _note_drop(msg='', key=None, n=0, names=(), fmt=None):
    """Notes accumulate while fresh: a counted note (key) adds its count and names to the one already showing, a
    plain message is its own note, and the line shows every fresh note, so one never hides another."""
    now = time.monotonic()
    parts = {k: v for k, v in S.drop_parts.items() if now - v['at'] < DROP_NOTICE_SECONDS}
    k = key or msg
    p = parts.get(k) or {'n': 0, 'names': set(), 'fmt': fmt, 'msg': msg}
    p['n'] += n
    p['names'] |= set(names)
    p['at'] = now
    parts[k] = p
    S.drop_parts = parts
    S.drop_note = '; '.join((v['fmt'] % (v['n'], ', '.join(sorted(v['names']))) if v['fmt'] and '%s' in v['fmt']
                             else v['fmt'] % v['n'] if v['fmt'] else v['msg']) for v in parts.values())
    S.drop_at = now


def _note_not_live(beta, legacy):
    """Worker only: a 200 that named beta or legacy events (cleared from the queue) is said on the EDMC line."""
    if beta + legacy > 0:
        _note_drop(key='notlive', n=beta + legacy, fmt=NOT_LIVE_NOTE)


def _sendable(e):
    """False for an event that cannot be encoded as the site needs: not valid JSON (NaN, Infinity) or too big."""
    try:
        return len(json.dumps(e, allow_nan=False)) <= EVENT_MAX_BYTES
    except (TypeError, ValueError):
        return False


def _note_reach(ok, fatal):
    """Worker: track how long the site has been unreachable, or answering with errors (plain state, no Tk)."""
    if ok or fatal or S.conflict:
        S.down_since = S.trouble_since = None
    elif S.answered:
        if S.trouble_since is None:
            S.trouble_since = time.monotonic()
        S.down_since = None
    else:
        if S.down_since is None:
            S.down_since = time.monotonic()


def _set_retry(fails, attempt):
    """Schedule the next try. The wait itself lives in _worker, where a stop or a new key can end it.
    Retry-After from the site is honoured and cannot be pulled in; a plain back-off can be pulled in
    to URGENT_RETRY after the attempt by a new urgent event, never closer."""
    now = time.monotonic()
    if S.retry_after:
        S.not_before = S.wake_floor = now + S.retry_after
    else:
        S.not_before = now + BACKOFF[min(fails - 1, len(BACKOFF) - 1)]
        S.wake_floor = min(S.not_before, attempt + URGENT_RETRY)


def _refuse(note):
    logger.warning('sending paused: %s', S.site_note or note)
    S.refused = True
    if S.site_note:
        S.status = 'check the site address'
        S.detail = S.site_note
        return
    S.status = 'key refused'
    S.detail = 'Key refused - paste a new key in Settings (%s)' % note


def _trim_pending():
    """Caller holds S.lock. Over MAX_QUEUE, remove events down to TRIM_TO, oldest first within each
    tier, never reordering: 1) pickups before a later Materials of the same commander (the server
    replaces its inventory with that snapshot, so they change nothing); 2) other pickups and
    movement; 3) anything. Tiers 2 and 3 lose something and are counted in S.dropped."""
    events = S.pending
    if len(events) <= MAX_QUEUE:
        return
    bulk = ('MaterialCollected', 'MaterialDiscarded')
    last_snapshot = {}
    for i, e in enumerate(events):
        if e.get('event') == 'Materials':
            last_snapshot[e.get('_fid')] = i
    tiers = ([], [], [])
    for i, e in enumerate(events):
        name = e.get('event')
        if name in bulk and i < last_snapshot.get(e.get('_fid'), -1):
            tiers[0].append(i)
        elif name in bulk or name in MOVES:
            tiers[1].append(i)
        else:
            tiers[2].append(i)
    need = len(events) - TRIM_TO
    gone = set()
    for n, tier in enumerate(tiers):
        take = tier[:max(0, need - len(gone))]
        gone.update(take)
        if n:
            S.dropped += len(take)
            if take:
                _note_drop('the queue was full, so some older events were dropped')
    for i in gone:
        S.retrying.pop(id(events[i]), None)     # a trimmed retried event is no longer waiting to be retried
    S.pending = [e for i, e in enumerate(events) if i not in gone]


def _pick_batch():
    """Caller holds S.lock. Up to MAX_BATCH events of ONE commander (the first in the queue whose
    commander the site is not currently holding off), oldest first. A commander the site answered
    409 for is skipped for BLOCK_RETRY seconds so it cannot block the others behind it. Never a newer
    event of a commander ahead of an older one. An event that cannot be encoded is removed first."""
    now = time.monotonic()
    S.settle_at = 0.0
    for f in [f for f, t in S.blocked.items() if now - t >= BLOCK_RETRY]:
        del S.blocked[f]
    while True:     # each pass that finds an unencodable event removes it, so this ends
        batch = _pick_oldest()
        bad = [x for x in batch if not _sendable(x)]
        if not bad:
            return batch
        gone = {id(x) for x in bad}
        S.pending = [x for x in S.pending if id(x) not in gone]
        for i in gone:
            S.retrying.pop(i, None)
        S.unsaved = True
        _note_drop(key='toobig', n=len(bad), fmt='%d event(s) could not be sent (too large or not valid JSON); dropped')


def _settled(e, now):
    """False while e may still have an identical twin to come: it is in the newest second seen, and
    the last line of that second was seen under SETTLE_SECONDS ago."""
    newest_ts, newest_at = S.newest
    ts = e.get('timestamp')
    return not newest_ts or not isinstance(ts, str) or ts < newest_ts or now - newest_at >= SETTLE_SECONDS


def _pick_oldest():
    now = time.monotonic()
    for e in S.pending:
        f = e.get('_fid')
        if f in S.blocked or id(e) in S.suspects:
            continue
        ready = []
        for x in S.pending:
            if x.get('_fid') != f or id(x) in S.suspects:
                continue
            if not _settled(x, now):
                S.settle_at = S.newest[1] + SETTLE_SECONDS
                break
            ready.append(x)
        if not ready:
            continue
        limit = 1 if f in S.probe else S.batch_limit   # a 409'd commander is probed with one event
        batch = ready[:limit]
        last = batch[-1].get('timestamp')
        if len(ready) > len(batch) and ready[len(batch)].get('timestamp') == last:
            # identical events in one second look like a resend to the site, so a batch ends on a second boundary
            n = len(batch)
            while n > 0 and batch[n - 1].get('timestamp') == last:
                n -= 1
            if n:
                batch = batch[:n]
            else:
                batch = list(ready[:1])
                while len(batch) < len(ready) and ready[len(batch)].get('timestamp') == last:
                    batch.append(ready[len(batch)])
        return batch
    return []


def _wait_for_wake(timeout):
    """None to stop, True if any token other than 'save' arrived, 'save' if only 'save' tokens did,
    False on timeout. Any number of queued tokens is ONE pass: after the first, the rest are drained,
    so a burst of events is not a burst of requests."""
    try:
        token = S.outbox.get(timeout=timeout)
    except queue.Empty:
        return False
    if token is None or S.stop.is_set():
        return None
    result = 'save' if token == 'save' else True
    while True:
        try:
            token = S.outbox.get_nowait()
        except queue.Empty:
            return result
        if token is None:
            return None
        if token != 'save':
            result = True


def _game_running(now):
    return S.game_on and now - S.game_seen < GAME_QUIET_AFTER


def _idle_wait():
    return WAKE_SECONDS if _game_running(time.monotonic()) or S.pending else IDLE_WAKE_SECONDS


def _save_if_due(now):
    """Write the queue once the deadline an event armed has passed (worker, or saver thread during a send)."""
    due = S.save_due
    if due and now >= due:
        if S.unsaved:
            _save_queue()
        with S.lock:
            if S.save_due == due:           # an event that arrived during the write re-armed it: keep that
                S.save_due = 0.0


def _saver():
    """Its own thread, never tk, never the worker: the worker can sit inside one request for 40 s or more, and
    saves used to wait for it. Takes the same _SAVE_LOCK as every other save, so writes never race."""
    while not S.stop.wait(SAVER_TICK):
        try:
            _save_if_due(time.monotonic())
        except Exception:
            logger.exception('queue saver pass failed')


def _worker():
    fails = 0
    wake_at = None
    while not S.stop.is_set():
        attempt = time.monotonic()
        try:
            now = attempt
            if wake_at is None:
                wake_at = S.not_before if S.not_before > now else now + _idle_wait()
            timeout = wake_at - now
            if S.save_due:
                timeout = min(timeout, S.save_due - now)
            woke = _wait_for_wake(max(timeout, 0))
            if woke is None or S.stop.is_set():
                break
            now = attempt = time.monotonic()
            _save_if_due(now)
            if woke is not True and now < wake_at:
                continue    # a save-only wake never runs a pass and never pulls a back-off in
            wake_at = None
            if woke is True and now < S.not_before:
                S.not_before = max(S.wake_floor, now)   # an event may pull a back-off in, not past the floor
            if now < S.not_before:
                continue
            S.not_before = 0.0
            fails = _cycle(fails)
            _pass_ok()
        except Exception:
            wake_at = None
            fails = _pass_failed(fails, attempt)


def _pass_failed(fails, attempt):
    # A sender that dies stops draining the queue for good - a trade sits at
    # 'sent · 1 waiting' and the site never updates until EDMC restarts. No single bad pass
    # may kill the thread; back off and go round again. (The root cause of that failure mode,
    # a cross-thread Tkinter call, is fixed in _set_status; this is the backstop.)
    S.pass_errors += 1
    if S.pass_errors == 1:
        logger.exception('sender pass failed; backing off')
    else:
        logger.debug('sender pass failed again (%d in a row)', S.pass_errors, exc_info=True)
    fails += 1
    _set_retry(fails, attempt)
    S.stop.wait(1)   # never a hot loop, and a stop still ends it at once
    return fails


def _pass_ok():
    if S.pass_errors:
        logger.info('the sender is working again after %d failed passes', S.pass_errors)
        S.pass_errors = 0


def _count(v):
    return v if isinstance(v, int) and not isinstance(v, bool) and v > 0 else 0


def _site_text(s, limit=200):
    """Text from the site, safe to show or log: no control characters, at most `limit` characters."""
    s = ''.join(' ' if ord(c) < 32 or ord(c) == 127 else c for c in str(s))
    return s[:limit] + '...' if len(s) > limit else s


def _send(events):
    """Returns (ok, note, fatal). Replays are safe, so a non-fatal failure keeps the batch."""
    if S.unsaved:
        _save_queue()       # a slow request must not be the only thing standing between an event and its file
    host, key = S.host, S.key
    S.sent_with = (host, key)
    body = {'events': events, 'plugin_version': VERSION}
    S.conflict = False
    S.answered = False
    S.retry_after = None
    S.rejected = False
    S.server_error = False
    S.refused_now, S.refused_names, S.refused_evs = 0, [], []
    S.whole_refusal, S.applied_n = False, 0
    S.skipped_old = set()
    S.site_note = ''
    if not (host.startswith('https://') or (host.startswith('http://') and _is_loopback(host))):
        S.site_note = ('The site address is not https, so the key was not sent. '
                       'Check Site in Settings → EDMC-Engineering.')
        return False, S.site_note, True
    S.held_back = set()
    # The batch holds one commander (see _pick_batch); its events also each carry `_fid`.
    fid = events[0].get('_fid') if events else None
    if fid:
        body['fid'] = fid
    with S.lock:
        ack = S.dest_ack
    if ack is not None:
        body['ack'] = ack['id']
        body['copied'] = ack['copied']
    headers = {
        'content-type': 'application/json',
        'authorization': 'Bearer ' + key,
        'user-agent': '%s %s/%s' % (user_agent, PLUGIN_NAME.replace(' ', '-'), VERSION),
    }
    try:
        res = _SESSION.post(host + '/api/v1/ingest/events', data=json.dumps(body, allow_nan=False).encode('utf-8'),
                            headers=headers, timeout=TIMEOUT, allow_redirects=False, stream=True)
        S.answered = True
        code = res.status_code
        if 300 <= code < 400:
            # Never follow: the key must not be forwarded to whatever host Location names.
            try:
                target = urlsplit(urljoin(host, res.headers.get('Location') or '')).netloc[:60]
            except ValueError:
                target = ''
            S.site_note = ('The site redirected to %s; the key was not sent there. '
                           'Check Site in Settings → EDMC-Engineering.' % (target or 'another address'))
            res.close()
            return False, S.site_note, True
        if not 200 <= code < 300:
            return _http_failure(res, events)
        text = _read_body(res)
        if text is None:
            return False, 'the site sent a reply too large or too slow', False
        payload = json.loads(text or '{}')
        if not isinstance(payload, dict):
            return False, 'the site sent an unexpected reply', False
        applied = payload.get('applied')
        if isinstance(applied, bool) or not isinstance(applied, int):
            return False, 'the site sent an unexpected reply', False
        if events and not 0 <= applied <= len(events):
            return False, 'the site sent an unexpected reply', False
        skipped, refused, legacy, bad = (_count(payload.get(k)) for k in ('skipped', 'refused', 'legacy', 'bad'))
        note = '%d applied' % applied
        kinds = _skip_kinds(events, skipped, payload.get('skipped_events'))
        if kinds:
            known, _aside, S.skipped_old = kinds    # set-aside events are not "already known", and not mentioned
            known -= sum(1 for i in S.skipped_old if i in S.retrying)   # a retried too-old event is in the drop note
        else:
            known = skipped
        if known:
            note += ', %d already known' % known
        beta = payload.get('beta')
        if isinstance(beta, bool) or not isinstance(beta, int):
            beta = len(beta) if isinstance(beta, list) else 0
        if beta:
            note += ', %d from a beta session not stored' % beta
        _note_not_live(beta, legacy)
        unknown = payload.get('unknown_fids')
        if isinstance(unknown, list):
            S.held_back = {f for f in unknown if isinstance(f, str)}
        _offer_destination(_parse_destination(payload.get('destination')))
        # Only the ack THIS call sent may be cleared: a newer ack set by the UI meanwhile (a second
        # destination arriving and being shown before this response came back) must survive.
        with S.lock:
            if ack is not None and S.dest_ack is ack:
                S.dest_ack = None
        if events and refused > 0 and payload.get('retry_refused') is True:
            # The site will take a refused event again later (it remembers what it applied), so none of
            # this is a failure: the named events are parked by _cycle and retried, never dropped here.
            S.refused_evs = _refused_in(events, refused, payload.get('refused_events'))
            note += ', %d could not be stored' % refused
            S.applied_n = applied
            if applied == 0 and any(id(e) not in S.retrying for e in S.refused_evs):
                # Nothing stored, so the site is faulty, not the events: held and retried, nothing parked or counted.
                S.rejected, S.whole_refusal = 'could not apply', True
                return False, note, False
            return True, note, False
        S.applied_n = applied
        if events and applied == 0:
            if refused > 0:
                # A 200 that stored nothing because the handlers threw: handled like a rejected batch.
                S.rejected = 'could not apply'
                return False, note + ', %d could not be stored' % refused, False
            if not (skipped or beta or legacy or bad) and not (isinstance(unknown, list) and unknown):
                return False, 'the site sent an unexpected reply', False
        if events and applied > 0 and refused > 0:
            # Part stored, the rest named as refused: the stored ones prove the site works, so the
            # named ones are dropped by _cycle (with a note). Never reached when nothing was stored.
            S.refused_now = refused
            S.refused_names = [r.get('event') for r in (payload.get('refused_events') or [])
                               if isinstance(r, dict) and r.get('event') in WANTED]
            note += ', %d could not be stored' % refused
        return True, note, False
    except (requests.exceptions.RequestException, OSError) as err:
        return False, 'could not reach the site (%s)' % (getattr(err, 'reason', err),), False
    except ValueError as err:
        return False, 'the site sent something that is not JSON (%s)' % (err,), False


def _skip_kinds(events, skipped, listed):
    """(known, aside, old) from the reply's skipped_events, or None (0.6.5 behaviour) unless the list is wholly
    trustworthy: as long as `skipped`, unique int indexes into events, each entry matching its event."""
    if not isinstance(listed, list) or len(listed) != skipped:
        return None
    seen, known, aside, old = set(), 0, 0, set()
    for r in listed:
        i = r.get('i') if isinstance(r, dict) else None
        if isinstance(i, bool) or not isinstance(i, int) or not 0 <= i < len(events) or i in seen:
            return None
        ev = events[i]
        if r.get('event') != ev.get('event') or r.get('timestamp') != ev.get('timestamp'):
            return None
        seen.add(i)
        why = r.get('why')
        if why == 'superseded':
            aside += 1
        elif why in ('known', 'too-old'):
            known += 1
            if why == 'too-old':
                old.add(id(ev))
        else:
            return None
    return known, aside, old


def _refused_in(events, refused, named):
    """The events of this batch a 200 named as refused (event, timestamp, occ). When it names fewer than it
    counted, the whole batch. One matched too many is harmless: the site skips it when it comes round again."""
    named = [r for r in (named or []) if isinstance(r, dict)]
    if refused > len(named):
        return list(events)
    by_i, ok = set(), True
    for r in named:     # a new site gives each entry its position in the batch: match by that, exactly
        i = r.get('i')
        if (isinstance(i, bool) or not isinstance(i, int) or not 0 <= i < len(events) or i in by_i
                or r.get('event') != events[i].get('event') or r.get('timestamp') != events[i].get('timestamp')):
            ok = False
            break
        by_i.add(i)
    if ok and named:
        return [e for n, e in enumerate(events) if n in by_i]
    return [e for e in events if any(
        r.get('event') == e.get('event') and r.get('timestamp') == e.get('timestamp')
        and ('occ' not in r or r['occ'] == e.get('_occ')) for r in named)]


def _http_failure(res, events):
    """A non-2xx, non-3xx answer: (ok, note, fatal), with the same meaning as before."""
    code = res.status_code
    if events:      # an idle poll that fails would log every 10 s
        logger.warning('site answered %d to a batch of %d events', code, len(events))
    info = _error_info(_read_body(res) or '')
    text = _site_text(info.get('error')) if isinstance(info.get('error'), str) else ''
    # Only this server's own JSON refusal means a bad key. A 403 with any other body (a
    # Cloudflare challenge or WAF page) is transient: keep the batch and back off.
    if code in (401, 403) and info.get('code') in REFUSAL_CODES:
        return False, 'the site refused the key (%d): %s' % (code, text), True
    if code in (429, 503):
        raw = str((getattr(res, 'headers', None) or {}).get('Retry-After') or '').strip()
        if raw.isascii() and raw.isdigit() and int(raw) >= 1:
            S.retry_after = min(int(raw), RETRY_AFTER_MAX)   # an HTTP-date or junk is ignored
    if code in (400, 413) and isinstance(info.get('error'), str):
        S.rejected = code   # the site's own JSON refusal, not a proxy's page
    if code == 500 and isinstance(info.get('error'), str):
        S.server_error = True   # the site's own JSON 500, not a proxy's page
    if code in (401, 403):
        return False, 'the site answered %d without its own refusal (a block page?)' % code, False
    if code == 409:
        # Nothing stored for this commander yet. Keep the batch: it will apply once the
        # player uploads a journal folder on the site.
        S.conflict = True
        return False, 'nothing to update yet — upload a journal folder on the site first', False
    return False, 'the site said %d: %s' % (code, text), False


def _read_body(res):
    """The reply body as text, or None when it passes BODY_MAX bytes or TIMEOUT seconds. Always closes res."""
    try:
        chunks, size, end = [], 0, time.monotonic() + TIMEOUT
        for chunk in res.iter_content(65536):
            chunks.append(chunk)
            size += len(chunk)
            if size > BODY_MAX or time.monotonic() > end:
                return None
        return b''.join(chunks).decode('utf-8', errors='replace')
    finally:
        res.close()


def _error_info(text):
    try:
        info = json.loads(text or '{}')
        return info if isinstance(info, dict) else {}
    except Exception:
        return {}


# ---------- destinations (0.3.0) ----------
# The site can ask this plugin to show and copy a place. It never sets anything in game: it shows
# a line on EDMC's main window and puts the system name on the clipboard, so it can be pasted into
# the galaxy map. Everything below either never touches Tk (the worker thread) or only ever runs on
# the UI thread (the gotcha this whole file is built around — see the comment in _set_status).

def _bad_name(s):
    return any(ord(c) < 32 or ord(c) == 127 for c in s)


def _parse_destination(value):
    """A destination the server's ingest reply carried, or None — never raises, so a malformed
    reply can never break the worker. `value` must be a dict: `id` a 1-40 character str; `system`
    a non-empty str after strip, at most DEST_MAX characters, no control characters; `station` the
    same rules but may be empty, or missing (then '')."""
    if not isinstance(value, dict):
        return None
    did = value.get('id')
    if not isinstance(did, str) or not (1 <= len(did) <= 40) or _bad_name(did):
        return None
    system = value.get('system')
    if not isinstance(system, str):
        return None
    system = system.strip()
    if not system or len(system) > DEST_MAX or _bad_name(system):
        return None
    station = value.get('station', '')
    if station is None:
        station = ''
    if not isinstance(station, str) or len(station) > DEST_MAX or _bad_name(station):
        return None
    return {'id': did, 'system': system, 'station': station}


def _destination_line(d, copied):
    """'Destination: Liu Yines, copied (Baraniecki Orbital)' — no station drops the '(…)'. A copy
    that failed says so plainly instead, with the station still named."""
    tail = ' (%s)' % d['station'] if d.get('station') else ''
    if copied:
        return 'Destination: %s, copied%s' % (d['system'], tail)
    return 'Destination: %s (could not copy: type it in)%s' % (d['system'], tail)


def _offer_destination(d):
    """Worker thread only, touches no Tk. Queues a genuinely new destination for the UI; the same
    id offered again (every wake, until the ack lands) is dropped here so it is not re-copied over
    and over onto the clipboard."""
    if d and d['id'] != S.dest_seen:
        S.dest_seen = d['id']
        S.dest_in.put(d)


def _copy_to_clipboard(text):
    """UI thread only. False on anything that stops the copy — no frame yet, or Tk refusing —
    never raises, since a failed copy is a normal, shown outcome, not a bug."""
    if S.frame is None:
        return False
    try:
        S.frame.clipboard_clear()
        S.frame.clipboard_append(text)
        return True
    except (tk.TclError, AttributeError):
        return False


def _copy_again():
    """UI thread only (runs from the Copy button). Copies the shown system again and updates the
    line. Sends no ack (the site already has one) and touches no worker state."""
    d = S.dest_shown
    if not d:
        return
    copied = _copy_to_clipboard(d['system'])
    S.dest_text = _destination_line(d, copied)
    if S.dest_var is not None:
        S.dest_var.set(S.dest_text)


def _take_destination():
    """Main-thread repaint, called from _tick before _set_status: drains the worker's queue,
    keeping only the newest destination, shows and copies it, and hands the worker an ack to send
    back. Off the UI thread this is a no-op, the same rule and the same reason as _set_status."""
    if S.ui_thread is None or threading.current_thread() is not S.ui_thread:
        return
    d = None
    while True:
        try:
            d = S.dest_in.get_nowait()
        except queue.Empty:
            break
    if d is None:
        return
    copied = _copy_to_clipboard(d['system'])
    S.dest_shown = d
    S.dest_text = _destination_line(d, copied)
    if S.dest_var is not None:
        S.dest_var.set(S.dest_text)
    if S.dest_label is not None:
        S.dest_label.grid()
    if S.dest_button is not None:
        S.dest_button.grid()
    with S.lock:
        S.dest_ack = {'id': d['id'], 'copied': copied}
    S.outbox.put('send')   # a thread-safe queue, not Tk: wakes the worker so the ack lands in about a second


# ---------- settings, queue, status ----------

def _key_ok(k):
    return bool(re.fullmatch(r'edeng_[A-Za-z0-9_-]+', k or ''))


def _load_settings():
    if config:
        S.host = _clean_host(config.get_str('edeng_host') or '') or DEFAULT_HOST
        S.key = (config.get_str('edeng_key') or '').strip()
        if not _key_ok(S.key):
            S.key = ''


def _is_loopback(url):
    try:
        return urlsplit(url).hostname in ('localhost', '127.0.0.1', '::1')
    except ValueError:
        return False


def _clean_host(host):
    host = (host or '').strip().rstrip('/')
    if not host:
        return ''
    scheme, sep, rest = host.partition('://')
    if not sep:
        scheme, rest = 'https', host
    if scheme.lower() == 'http' and _is_loopback('http://' + rest):
        return 'http://' + rest
    return 'https://' + rest


_SESSION = requests.Session()


# ---------- seeding from the journal on start ----------

# EDMC forwards events written AFTER it is running, not the session so far. A plugin started
# mid-session (or configured after the game was already open) therefore never sees the game's
# Materials snapshot, nor the pickups before it connected — so the server rolls live deltas onto a
# stale base and your held materials read a week old until you reload the game. On start we read the
# newest journal ourselves and queue the events the server wants; the server dedupes by timestamp, so
# only what it has not already seen is applied, and the materials it missed are filled in with the
# game's own (correctly localised) names. Entirely best-effort: any failure leaves behaviour unchanged.

def _journal_dir():
    if config:
        try:
            d = config.get_str('journaldir') or getattr(config, 'default_journal_dir', '') or ''
            if d:
                return d
        except Exception:
            logger.debug('journaldir lookup failed', exc_info=True)
    return os.path.join(os.path.expanduser('~'), 'Saved Games', 'Frontier Developments', 'Elite Dangerous')


def _newest_journal(jdir):
    try:
        paths = [os.path.join(jdir, f) for f in os.listdir(jdir)
                 if f.startswith('Journal') and f.endswith('.log')]
    except OSError:
        return None
    if not paths:
        return None
    paths.sort(key=lambda p: os.path.getmtime(p))   # by mtime, robust across the two name formats
    return paths[-1]


def _shape(entry, route):
    """The one place an event gets the form it is sent in (live and seed). Returns a new dict, or None
    for "do not send". Never adds _fid; callers stamp after shaping. route is the NavRoute source
    when the entry carries none (None means unknown)."""
    event = entry.get('event')
    if event == 'MissionCompleted':
        # Sent only for its material rewards, trimmed to what the tool reads.
        if not entry.get('MaterialsReward'):
            return None
        return {k: entry[k] for k in ('timestamp', 'event', 'MaterialsReward') if k in entry}
    if event == 'NavRoute':
        # The game writes the route to NavRoute.json and only a stub line to the journal.
        src = entry.get('Route') or route
        if src is None:
            return None
        return dict(entry, Route=[{'StarSystem': r.get('StarSystem'), 'StarPos': r.get('StarPos')}
                                  for r in src if isinstance(r, dict)])
    if event in ('Location', 'FSDJump', 'CarrierJump', 'Docked'):
        return {k: entry[k] for k in MOVE_KEEP if k in entry}
    if event == 'Undocked':
        return {k: entry[k] for k in UNDOCKED_KEEP if k in entry}
    if event == 'FSDTarget':
        return {k: entry[k] for k in FSDTARGET_KEEP if k in entry}
    if event == 'LoadGame':
        return {k: entry[k] for k in LOADGAME_KEEP if k in entry}
    if event == 'Statistics':
        out = {k: entry[k] for k in ('timestamp', 'event') if k in entry}
        for grp, keys in STATS_KEEP.items():
            g = entry.get(grp)
            if isinstance(g, dict):
                kept = {k: g[k] for k in keys if k in g}
                if kept:
                    out[grp] = kept
        return out
    return entry


def _route_from(entry, navroute):
    """The route for a stub NavRoute line: NavRoute.json's (or EDMC's copy of it) only when it is this
    route, the same event and the same timestamp as the stub. Otherwise None (unknown, not empty)."""
    if (entry.get('event') == 'NavRoute' and isinstance(navroute, dict)
            and navroute.get('event') == 'NavRoute'
            and navroute.get('timestamp') == entry.get('timestamp')
            and isinstance(navroute.get('Route'), list)):
        return navroute['Route']
    return None


def _stamped_events_from_journal_text(text, navroute=None):
    """WANTED events from a journal, from the last Materials snapshot onward (that snapshot is the
    authoritative base, and the lines after it are the deltas; with none, all WANTED events), each
    stamped with the commander in force at that point in the file. Walks the whole file, following every
    Commander/LoadGame line, so a relog
    A to B inside one journal stamps B's Materials and EngineerProgress with B, even though the
    Commander line sits before the slice. Each event is shaped by _shape, the same as the live path.
    navroute is the parsed NavRoute.json; it is used only for the stub NavRoute line of the same
    second. Returns (events, last FID seen)."""
    fid = None
    evs = []
    header = None
    counts = ['', {}]
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            ev = json.loads(line)
        except ValueError:
            continue
        if not isinstance(ev, dict):
            continue
        if ev.get('event') in ('LoadGame', 'Commander') and ev.get('FID'):
            fid = ev['FID']
        if ev.get('event') in WANTED:
            ev = _shape(ev, _route_from(ev, navroute))
            if ev is None:
                continue
            ev = dict(ev, _occ=_next_occ(counts, ev))   # as journal_entry does, so a seeded copy equals its live copy
            if ev.get('event') == 'Fileheader':
                header = ev     # held until the next event, as in journal_entry
                continue
            if header is not None:
                evs.append(dict(header, _fid=fid) if fid else header)
                header = None
            evs.append(dict(ev, _fid=fid) if fid else ev)
    if header is not None:
        evs.append(dict(header, _fid=fid) if fid else header)
    last_materials = None
    for i, ev in enumerate(evs):
        if ev.get('event') == 'Materials':
            last_materials = i
    return (evs if last_materials is None else evs[last_materials:]), fid


def _beta_journal(text):
    """True if a Fileheader or LoadGame line says the game version is a beta or alpha (the site's test)."""
    for line in text.splitlines():
        if '"Fileheader"' not in line and '"LoadGame"' not in line:
            continue
        try:
            ev = json.loads(line)
        except ValueError:
            continue
        if (isinstance(ev, dict) and ev.get('event') in ('Fileheader', 'LoadGame')
                and isinstance(ev.get('gameversion'), str)):
            v = ev['gameversion'].lower()
            if 'beta' in v or 'alpha' in v:
                return True
    return False


def _merge_key(e):
    """An event's identity for the merge: its JSON without `_occ`, so a queue file from before 0.6.5 still matches."""
    return json.dumps({k: v for k, v in e.items() if k != '_occ'}, sort_keys=True)


def _merge_into_pending(seeded, keys=None):
    """Caller holds S.lock. Places events by timestamp among S.pending; only as many copies as are
    already queued are skipped (a multiset)."""
    if keys is None:
        keys = [_merge_key(e) for e in seeded]
    counts = {}
    for q in S.pending:
        k = _merge_key(q)
        counts[k] = counts.get(k, 0) + 1
    top, prefix = '', []    # prefix[i] = the latest timestamp among the queue's first i+1 events
    for q in S.pending:
        top = max(top, q.get('timestamp') or '')
        prefix.append(top)
    slots = {}
    for e, k in zip(seeded, keys):
        if counts.get(k, 0) > 0:
            counts[k] -= 1      # a multiset: only as many as are already queued are skipped
            continue
        ts = e.get('timestamp')
        slots.setdefault(bisect.bisect_right(prefix, ts) if ts else len(S.pending), []).append(e)
    merged = []
    for i, q in enumerate(S.pending):
        merged.extend(slots.get(i, ()))
        merged.append(q)
    merged.extend(slots.get(len(S.pending), ()))
    S.pending = merged


def _seed_from_journal():
    try:
        fid_before, gen_before = S.fid, S.fid_gen    # taken before any file read: a live Commander during it wins
        path = _newest_journal(_journal_dir())
        if not path:
            return True
        with open(path, 'r', encoding='utf-8') as fh:
            text = fh.read()
        if _beta_journal(text):
            logger.debug('newest journal is a beta session; not seeding from it')
            return True
        try:
            with open(os.path.join(os.path.dirname(path), 'NavRoute.json'), 'r', encoding='utf-8') as fh:
                navroute = json.load(fh)
        except (OSError, ValueError):
            navroute = None
        seeded, fid = _stamped_events_from_journal_text(text, navroute)
        if not seeded:
            return True
        stamp = _last_stamp(text)
        if stamp and stamp > S.newest[0]:
            S.newest = (stamp, time.monotonic())    # so the newest second waits for a live twin (benign race)
        if fid and S.fid is fid_before and S.fid_gen == gen_before:
            S.fid = fid    # a LoadGame/Commander that arrived live meanwhile wins
        keys = [_merge_key(e) for e in seeded]    # computed outside the lock
        with S.lock:
            _merge_into_pending(seeded, keys)
            S.unsaved = True
            if not S.save_due:
                S.save_due = time.monotonic() + SAVE_DELAY
            _trim_pending()
        S.outbox.put('send')
        return True
    except Exception:
        logger.exception('startup seeding failed')   # it must never break the plugin
        return False


def _last_stamp(text):
    """Timestamp of the last line that parses as a dict with a str timestamp (any event), else ''."""
    for line in reversed(text.splitlines()):
        try:
            d = json.loads(line)
        except ValueError:
            continue
        if isinstance(d, dict) and isinstance(d.get('timestamp'), str):
            return d['timestamp']
    return ''


def _read_queue_file():
    """('missing'|'locked'|'bad'|'ok', held). Only 'ok' carries the parsed file."""
    try:
        with open(S.queue_file, 'r', encoding='utf-8') as fh:
            held = json.load(fh)
    except FileNotFoundError:
        return 'missing', None
    except OSError:
        return 'locked', None
    except ValueError:
        return 'bad', None
    if not isinstance(held, dict) or not isinstance(held.get('events'), list):
        return 'bad', None
    return 'ok', held


def _wanted_events(held):
    """The file's events that are still wanted, old single-fid files restamped; logs what it drops."""
    events = held['events']
    ver = held.get('version')
    if isinstance(ver, int) and not isinstance(ver, bool) and ver > QUEUE_VERSION:
        logger.warning('the queue file was written by a newer plugin; loading what this version understands')
    kept = [e for e in events if isinstance(e, dict) and e.get('event') in WANTED]
    gone = [e.get('event') for e in events if isinstance(e, dict) and e.get('event') not in WANTED]
    if gone:
        logger.info('%d saved event(s) are no longer wanted and were dropped: %s', len(gone),
                    ', '.join(sorted({str(n) for n in gone})))
    # A queue written before per-event FIDs carries one fid for the whole file: the best
    # available owner for those events. New events are stamped as they are queued.
    if held.get('fid'):
        kept = [e if '_fid' in e else dict(e, _fid=held['fid']) for e in kept]
    return kept


def _set_aside():
    """Rename an unreadable queue file out of the way (never deleted). True if it was kept."""
    folder = os.path.dirname(S.queue_file)
    base = 'queue.json.unreadable-' + time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())
    name, n = base, 1
    while os.path.exists(os.path.join(folder, name)):    # a second file in the same second never replaces the first
        n += 1
        name = '%s-%d' % (base, n)
    try:
        os.replace(S.queue_file, os.path.join(folder, name))
    except OSError:
        logger.warning('could not set the unreadable queue file aside', exc_info=True)
        return False
    logger.warning('the saved queue could not be read; it was kept as %s', name)
    _note_drop('the saved queue could not be read; it was kept as ' + name, key='setaside')
    return True


def _load_queue():
    """A batch that never got sent survives the game being closed."""
    state, held = _read_queue_file()
    if state == 'bad' and not _set_aside():
        state = 'locked'
    if state == 'ok':
        S.fid = held.get('fid') or S.fid
        S.pending = _wanted_events(held)
        with S.lock:
            _merge_parked(held)
    elif state == 'locked':
        S.pending = []
        S.queue_unread = True
        logger.warning('the saved queue %s could not be opened; it will not be overwritten', S.queue_file)
    else:
        logger.debug('no saved queue to load')
        S.pending = []
    _load_pending_files()


_SAVE_LOCK = threading.Lock()


def _save_queue(final=False):
    """One writer at a time. The clean-exit write (final=True) closes the file: the worker may still
    be mid-request, and anything it delivers after that is resent next start (the site skips replays)."""
    with _SAVE_LOCK:
        if S.closed:
            return
        events = parked = None
        try:
            if S.queue_unread:
                state, held = _read_queue_file()
                if state == 'bad' and not _set_aside():
                    state = 'locked'
                if state == 'locked':
                    raise OSError('the old queue file could not be read, so it was not overwritten')
                if state == 'ok':
                    old = _wanted_events(held)
                    with S.lock:
                        _merge_into_pending(old)
                        _merge_parked(held)
                        S.fid = S.fid or held.get('fid')
                        _trim_pending()
                S.queue_unread = False
            tmp = S.queue_file + '.tmp'
            with S.lock:
                events, parked = _queue_snapshot()
                S.unsaved = False
                S.save_due = 0.0
            with open(tmp, 'w', encoding='utf-8') as fh:
                json.dump({'version': QUEUE_VERSION, 'fid': S.fid, 'events': events, 'parked': parked}, fh)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, S.queue_file)
            for old in S.carried:       # their events are in the file just written
                try:
                    os.remove(old)
                except OSError:
                    logger.warning('could not remove %s', old, exc_info=True)
            S.carried = []
            _drop_session_pending()
            if final:
                S.closed = True
            S.unsent_warned = False
            S.unsent_state = None
            if S.save_failed:
                S.save_failed = False
                logger.info('the queue file is being saved again')
        except Exception as err:
            with S.lock:
                S.unsaved = True    # restored, not cleared after the write: journal_entry may have set it mid-write
                if S.save_due <= time.monotonic():      # unset, or an event-driven save that has come due
                    S.save_due = time.monotonic() + SAVE_RETRY     # nothing else would try again until a new event
            if S.save_failed:
                logger.debug('could not save the queue', exc_info=True)
            else:
                logger.warning('could not save the queue to %s: %s', S.queue_file, err, exc_info=True)
            S.save_failed = True
            _write_unsent(events, final, parked)       # a crash before the next save must lose nothing


def _drop_session_pending():
    """A queue write now holds everything this session's pending file held."""
    path, S.pending_file = S.pending_file, None
    if path:
        try:
            os.remove(path)
        except OSError:
            logger.warning('could not remove %s', path, exc_info=True)


def _write_unsent(events, final=False, parked=None):
    """The queue file is refused: keep this session's events in a file beside it, which
    _load_pending_files merges at the next start. One file per session, replaced by each failed save,
    written to a temp name and renamed so a crash never leaves a cut-off file. Never raises."""
    try:
        if events is None:
            with S.lock:
                events, parked = _queue_snapshot()
        if not events and not parked:
            S.unsent_state = None
            return
        folder = os.path.dirname(S.queue_file)
        if not S.pending_file:
            base = 'queue.json.pending-' + time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())
            name, n = base, 1
            while os.path.exists(os.path.join(folder, name)):
                n += 1
                name = '%s-%d' % (base, n)
            S.pending_file = os.path.join(folder, name)
        final_path = S.pending_file
        tmp = os.path.join(folder, 'queue.json.pendingtmp-' + os.path.basename(final_path))
        with open(tmp, 'w', encoding='utf-8') as fh:
            json.dump({'version': QUEUE_VERSION, 'fid': S.fid, 'events': events, 'parked': parked or []}, fh)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, final_path)
        S.unsent_state = 'kept'
        (logger.warning if final else logger.debug)(
            'the queue file could not be written; %d waiting event(s) were kept as %s',
            len(events), os.path.basename(final_path))
    except Exception:
        (logger.debug if S.unsent_warned and not final else logger.warning)(
            'could not keep the waiting events in a pending file', exc_info=True)
        S.unsent_warned = True
        S.unsent_state = 'lost'


def _read_held(path):
    """The parsed file if it is complete and holds an events list, else None."""
    try:
        with open(path, 'r', encoding='utf-8') as fh:
            held = json.load(fh)
    except (OSError, ValueError):
        return None
    return held if isinstance(held, dict) and isinstance(held.get('events'), list) else None


def _load_pending_files():
    """Merge the files _write_unsent left behind into the queue, including a complete
    queue.json.pendingtmp-* left by a crash between its sync and its rename. They are removed only
    once a queue write has carried their events. A cut-off temp file holds nothing a complete pending
    file of the same session lacks, so it goes only when that file exists; otherwise it stays, with a warning."""
    folder = os.path.dirname(S.queue_file)
    try:
        names = sorted(f for f in os.listdir(folder) if f.startswith(('queue.json.pending-', 'queue.json.pendingtmp-')))
    except OSError:
        return
    for name in names:
        path = os.path.join(folder, name)
        temp = name.startswith('queue.json.pendingtmp-')
        held = _read_held(path)
        if held is None and temp:
            twin = os.path.join(folder, name[len('queue.json.pendingtmp-'):])
            if _read_held(twin) is not None:
                try:
                    os.remove(path)
                except OSError:
                    logger.warning('could not remove %s', name, exc_info=True)
                continue
        if held is None:
            logger.warning('could not read %s as a list of events; it is left where it is', name)
            continue
        with S.lock:
            _merge_into_pending(_wanted_events(held))
            _merge_parked(held)
            S.fid = S.fid or held.get('fid')
            S.unsaved = True
            if not S.save_due:
                S.save_due = time.monotonic() + SAVE_DELAY
            _trim_pending()
        S.carried.append(path)


SAVE_NOTE = "can't save the waiting events to disk; if EDMC closes now they are lost (see EDMC's log)"
SAVE_NOTE_EMPTY = "can't save the queue file; no events are waiting right now (see EDMC's log)"
SAVE_NOTE_KEPT = "can't save the queue file; the waiting events are kept in a side file (see EDMC's log)"

STATUS_TEXT = {
    'unconfigured': 'set the site and key in Settings → EDMC-Engineering',
    'site': 'check the site address',
    'refused': 'key refused, make a new one on the Account page',
    'internal': "internal error, will keep trying (see EDMC's log)",
    'unreachable': "can't reach the site, will keep trying",
    'checking': 'checking the new key',
    'no-data': 'upload your journal folder on the site first',
    'site-error': 'the site had a problem, will keep trying',
    'dropped': 'connected, some events were dropped',
    'setaside': 'connected, an unreadable queue file was set aside',
    'parked': 'connected, %d event(s) the site could not store yet; retrying',
    'connected': 'connected',
}


def _status_kind(now):
    """The one line's meaning, a pure function of state; first match wins."""
    if not S.host or not S.key:
        return 'unconfigured'
    if S.status == 'check the site address':
        return 'site'
    if S.status == 'key refused':
        return 'refused'
    if S.pass_errors >= 3:
        return 'internal'
    if S.down_since is not None and now - S.down_since >= UNREACHABLE_AFTER:
        return 'unreachable'
    if S.trouble_since is not None and now - S.trouble_since >= UNREACHABLE_AFTER:
        return 'site-error'
    if S.status == 'checking the new key':
        return 'checking'
    if S.status == 'waiting':
        return 'no-data'
    if S.parked or S.retrying:
        return 'parked'
    if S.drop_note and now - S.drop_at < DROP_NOTICE_SECONDS:
        fresh = {k for k, v in S.drop_parts.items() if now - v['at'] < DROP_NOTICE_SECONDS}
        return 'setaside' if fresh == {'setaside'} else 'dropped'
    return 'connected'


def _set_status():
    # Tkinter is single-threaded: a StringVar .set() from the sender thread raises ('main thread is
    # not in main loop') and, uncaught in _worker, kills that thread. Only the thread that built
    # the widgets renders; the worker updates plain state and _tick repaints within a second.
    # ui_thread is set first thing in plugin_app, before any widget exists, so until then (None) nothing
    # renders: nothing touches Tk off its own thread.
    if S.ui_thread is None or threading.current_thread() is not S.ui_thread:
        return
    now = time.monotonic()
    kind = _status_kind(now)
    lines = []
    if kind == 'site':
        lines.append(S.detail)
    elif S.drop_note and now - S.drop_at < DROP_NOTICE_SECONDS:
        lines.append(S.drop_note)
    if S.save_failed:
        lines.append({'kept': SAVE_NOTE_KEPT, 'lost': SAVE_NOTE}.get(S.unsent_state, SAVE_NOTE_EMPTY))
    detail = '\n'.join(l for l in lines if l)
    if S.status_var is not None:
        text = STATUS_TEXT[kind]
        if kind == 'parked':
            text = text % (len(S.parked) + len(S.retrying))
        S.status_var.set(text)
    if S.detail_var is not None:
        S.detail_var.set(detail)
    if S.detail_label is not None:
        if detail:
            S.detail_label.grid()
        else:
            S.detail_label.grid_remove()


def nb_frame(parent):
    """EDMC themes its preference frames through myNotebook; fall back to plain ttk."""
    try:
        import myNotebook as nb
        return nb.Frame(parent)
    except ImportError:
        return ttk.Frame(parent)
