# EDMC-Engineering

An EDMarketConnector (EDMC) plugin for https://ed.golegend.com, an invite-only Elite Dangerous engineering planner. It sends your journal events to that site as the game writes them, so your ships, materials and progress there stay current without you re-uploading your journal folder.

Version 0.6.8 (`VERSION` in `load.py`).

## Privacy

- The plugin sends your game journal events to ed.golegend.com as the game writes them: your commander, ships,
  materials, location, ranks, reputation, and five game statistics that engineers ask for (bounties claimed, combat bonds, tonnes mined, black markets traded with, markets traded with). Your credit totals and crime counts from Statistics are not sent. When EDMC starts, and when the site or key is changed, it also sends the wanted events in your newest journal from the last Materials snapshot on (never before both are set).
- The site keeps only what it reads from those events, not the events themselves. It also saves a dated copy of
  your commander after every upload, with no time limit.
- Nothing is sold or passed to any other service. Traffic goes through Cloudflare.
- To stop: clear the key in EDMC Settings or remove the plugin. Making a new key on the Account page cuts off the
  old one at once.
- The key is kept in EDMC's own settings, unencrypted, like any EDMC plugin setting, so anyone who can use your
  Windows account can read it. If it may have been seen, make a new key on the Account page.
- To delete: choose Delete account on the Account page. Backups may keep a copy for up to about 30 days.
- Questions: use the Feedback page on ed.golegend.com (https://ed.golegend.com/feedback).

## Installing it

The site is **https://ed.golegend.com**. You need an account there, and the plugin sends your
journal events to it.

1. **Install EDMC** (EDMarketConnector, downloads at https://github.com/EDCD/EDMarketConnector/releases) if you do not have it. The plugin uses `config.get_str`
   and the parsed `NavRoute` that EDMC hands to plugins, which are EDMC 5.x features. I have not tested
   which 5.x release is the oldest that works; use the current stable EDMC.
2. **Get an account.** Registration needs an invite code from an existing member: they open
   **Account**, then **Invite someone**, and send you the code. A code works once and lasts 7
   days; each member can invite one person. On https://ed.golegend.com choose **Have an invite
   code? Create an account.** and enter it.
3. **Get `load.py`.** The plugin is published at https://github.com/majorxp00/EDMC-Engineering.
   Download `load.py` (and this README) from there; `load.py` is the single file EDMC needs.
4. **Upload your journal folder** on the site once. Live events are applied on top of what is
   already stored, so there has to be something to apply them to; until then the plugin holds its
   batch and says so.
5. **Get your plugin key.** On the site open **Account** and choose **Create key** (**Regenerate key** if you already have one). It is **shown
   once**, so copy it now. Regenerating kills the old key immediately.
6. **Put the plugin in EDMC.** In EDMC: **File -> Settings -> Plugins -> Open**. That opens the
   plugins folder. Make a folder inside it called `EDMC-Engineering` and copy `load.py` into it.
7. **Restart EDMC.**
8. **File -> Settings -> EDMC-Engineering** (the tab is named after the plugin): **Site** is pre-filled with
   `https://ed.golegend.com` (leave it; clear the box and it comes back; an `http://` address is changed to `https://`); in **Plugin key** paste the key (it starts with `edeng_`; anything else is ignored and the key already saved is kept; clear the box to remove it). Close Settings. The line then shows `checking the new key` for a moment while the site checks it.

**Is it working?** EDMC's main window gets one line, `Engineering:` with a status beside it, and it
changes only when you have something to do. `connected` means all is well and stays that way while
you play (no counts of events sent; the per-batch figures stay in the plugin's own state and are not shown). While the site is refusing some events and the plugin is holding them to try again, the line reads `connected, N event(s) the site could not store yet; retrying`. A second
line appears under it only to say something you should know, such as the queue file not saving. `upload your journal folder on the site first` means the site
has nothing stored for you yet (step 4). `can't reach the site, will keep trying` appears only after
several failed tries in a row; the plugin keeps the events and sends them when the site answers.
`the site had a problem, will keep trying` means the site answers but with an error, after several tries; the
events are kept the same way.
`internal error, will keep trying (see EDMC's log)` shows after three sender passes in a row fail inside the plugin itself; the log has the first traceback, and the line clears when a pass works again.
`key refused, make a new one on the Account page` means paste a new key in **Settings ->
EDMC-Engineering**; no restart is needed. `set the site and key in Settings -> EDMC-Engineering`
means one of them is missing. If the site address is wrong (a redirect, or not https) the line reads
`check the site address` with the reason under it. If events had to be dropped the line reads `connected, some events were dropped`, with a note under it, for 10
minutes (while events are parked, the line shows the parked count instead, and the note still appears under it). Notes pile up while they are showing: a second drop adds its count and names to the first, and a beta or legacy note stays beside a drop note. If more than 1,000 refused events are waiting, the oldest are dropped and the note says the plugin dropped them because too many were waiting. A queue file that could not be read is renamed, never deleted, and the line reads `connected, an unreadable queue file was set aside`.

**Updating:** close EDMC with its window's X so it saves its settings (do not kill it from Task
Manager), replace `load.py` in the `EDMC-Engineering` folder with the new one, and start EDMC
again. The key and site are kept, and so is the queue file beside the plugin. If the Engineering line is missing afterwards,
EDMC's debug log has the error (see EDMC's Troubleshooting page: https://github.com/EDCD/EDMarketConnector/wiki/Troubleshooting).
If your plugins folder has an `ED Engineering` folder from an earlier version: close EDMC, make the
`EDMC-Engineering` folder, copy the new `load.py` and the old folder's `queue.json` into it, then rename
the old folder to `ED Engineering.disabled` (EDMC skips it) or delete it. Two copies
must never run at once. The key and Site are kept: EDMC stores them, not the folder.

**Uninstalling:** close EDMC, then remove the `EDMC-Engineering` folder (or rename it to
`EDMC-Engineering.disabled`). To cut the key off as well, make a new key on the site's Account page; that voids the
old one. There is no "Revoke key" button yet.
Removing the folder does not clear the Site and key: EDMC keeps them in its own settings (`edeng_host` and
`edeng_key`) and the plugin has no button to clear them, so they stay there.

**Problems:** report them at https://github.com/majorxp00/EDMC-Engineering/issues. For a security
problem, members can use the Feedback page on ed.golegend.com; anyone else can open an issue that says
only that there is a security report, with no details, and the maintainer will arrange a private route.

## What it sends, and what it does not

Journal events of the kinds the site uses, most of them whole (including `Commander`, which carries your in-game name):

| | |
|---|---|
| who you are | `Fileheader` `Commander` `LoadGame` (only your commander name and id, credits, current ship type and id, game mode, Horizons/Odyssey flags and game version) |
| standing | `EngineerProgress` `EngineerCraft` |
| materials | `Materials` `MaterialCollected` `MaterialDiscarded` `MaterialTrade` `Synthesis` `TechnologyBroker` `MissionCompleted` (material rewards only) `EngineerContribution` `ScientificResearch` |
| ships | `Loadout` `ShipyardSwap` `StoredShips` |
| ship purchases and sales | `ShipyardBuy` `ShipyardSell` `SellShipOnRebuy` (whole events, sent without waiting for the batch) |
| where you are | `Location` `FSDJump` `CarrierJump` `Docked` `Undocked` (`Location` `FSDJump` `CarrierJump` `Docked` carry only the system, its position, the station, its type, services and economies, the market id, and whether docked; `Undocked` carries only the time) |
| where you are going | `NavRoute` (the plotted route's systems and positions) `NavRouteClear` `FSDTarget` (the target system's name and the jumps left) |
| progress | `Rank` `Progress` `Reputation` `Statistics` (only bounties claimed, combat bonds, tonnes mined, black markets traded with and markets traded with; not your credits, crimes or any other total) |

Plotted routes are sent so the site can say how many jumps are left to a destination. Jumps, docking and routes are
not batched: each goes a few seconds after the game writes it (see "Identical events" below), and
the site's page then shows it at its next refresh.

Beyond the events, a check-in carries the plugin key, the plugin's own version, an
acknowledgement of a destination it has already shown (with a flag saying whether the copy worked), and
a user-agent that includes EDMC's own name and version.
When it has events to send, it also adds the commander's id, and every event carries that id too, once the plugin knows it.
Besides the game's journal and its NavRoute.json it reads only its own queue file and EDMC's own settings.
Live beta sessions, and a beta journal read at startup, are skipped by the plugin. Legacy sessions are sent and refused by the site. When the site refuses any events as beta or legacy, the EDMC line says how many, for 10 minutes.

## Destinations

The site can send one place to this plugin — a system, and optionally a station — for a click on
the site to show up here. The plugin shows it on EDMC's main window as `Destination: <system>,
copied (<station>)` and copies the system name to the clipboard, so it can be pasted into the
in-game galaxy map. If the copy itself fails, the line says so instead: `Destination: <system>
(could not copy: type it in) (<station>)`. A **Copy** button beside the line copies the system name again, for when something else has been copied since. The copy happens by itself when a destination arrives, and replaces whatever was on the clipboard. Only a place sent from your own signed-in account can arrive; a plugin key cannot send one. If EDMC restarts before the site has its acknowledgement, the same place is copied again, for up to 10 minutes after it was sent. **Nothing is set in game.** The plugin only shows and
copies.

To make this possible, the plugin checks in with the site every 10 seconds while the game is running (or until 15 minutes pass with no new journal line, as after a crash)
(or events are waiting; when events are waiting and the site is failing, retries follow the back-off in the table below, while an idle check-in keeps its own pace), and every 2 minutes while EDMC is open without the game, even when there is nothing to send — that check-in carries only the key, the plugin's
version, and, once a destination has been shown, an ack and its copied flag. That is what lets the site say "EDMC may be
closed" rather than staying silent until the game next writes an event.

## How it behaves when things go wrong

| | |
|---|---|
| Site unreachable | keeps the batch and retries, backing off 5s → 15s → 60s → 5min |
| Queue full (5,000 waiting) | drops the least useful events down to 4,500: pickups an inventory snapshot already covers first, then other pickups and movement, then the oldest. The EDMC line speaks only when it had to drop more than the pickups a snapshot already covers |
| Site rejects a batch (400/413 with its own error) | sends it in halves 2 seconds apart down to one event. If that is still refused, it keeps everything and retries with the usual back-off until the site takes it. Nothing is dropped. A 400/413 that is not the site's own JSON is retried like any outage |
| Site answers but could not store any of a batch (a 200 that names refused events) | from 0.6.5, a site that offers retries (it says `retry_refused`) is not treated as failing. From 0.6.7 a batch the site stored none of is held and retried with back-off, nothing set aside (except a lone event with nothing else queued, which is set aside after about an hour), because that is a site fault and not bad events; the EDMC line then says the site had a problem. An event refused alone for an hour is tested by itself and set aside only if the events after it are stored, or if nothing else is waiting. Events refused beside stored ones are set aside, and each is tried again after 10 minutes, 30 minutes, 1 hour, then every 2 hours, and while any are waiting the EDMC line reads "connected, N event(s) the site could not store yet; retrying". Newer events of that commander are not held up. A refused event is dropped, with a note on the EDMC line, only if the site refuses it again 24 hours or more after it first did (about 16 tries); the waiting ones are kept in the queue file. A retried event the site says it can no longer check (it answers `too-old` in `skipped_events`) is dropped with the same note. A site without `retry_refused` is handled as in 0.6.4: a batch it stored nothing of is kept and retried, and refused events beside stored ones are dropped with a note |
| Site has an internal error on a batch (500 with its own error) | keeps the batch and retries with the usual back-off for as long as it lasts. A 500 page that is not the site's own is retried like any outage |
| One commander held, another waiting | the held commander is set aside for a minute while the other one is sent, so one commander's refused event never stalls another's |
| An event that cannot be sent at all (over 512 KB, or a number JSON cannot carry) | dropped, and the EDMC line says so |
| Site asks to slow down (429/503 with Retry-After) | waits that many seconds (up to an hour); without the header, the usual back-off |
| Key refused (401/403 with the site's own JSON refusal) | **stops**, and says the key was refused. A revoked key stays revoked; retrying it would only hammer the site. A 401/403 that is not the site's own JSON (a block page, say) is retried like any outage |
| Site redirects (3xx) | **stops** and says to check the site address; the key is never sent on to the new address |
| Nothing stored yet (409) | keeps the batch and says to upload a journal folder on the site first; the queue file is updated while it waits |
| Identical events written in one second | each event carries `_occ`, its number among identical events of that second (0 for the first, 1 for its twin), so the site can tell a twin from a resend even when it arrives later. Events of the latest journal second still wait until the game writes a later line or 2 seconds pass, so identical events usually travel together; a batch ends on a second boundary. An event of the latest second waits until 2 seconds after the last line of that second, so urgent events (a material trade, a jump) usually reach the site 2 to 4 seconds after the game writes them; a later line sends them at once |
| Game or EDMC closed mid-flight | the queue is in a file beside the plugin and is picked up next start. The file is rewritten within about 2 seconds of each new event, so if EDMC is killed rather than closed only the last 2 to 3 seconds can be lost, however slow the site is (a separate saver thread writes the file while a request is still waiting, and the queue is also written just before each send), except those the startup read of the newest journal finds again. If the queue file cannot be written (a read-only file, or one another program holds open) while EDMC is running, the EDMC line says so under it (the waiting events are kept in a side file beside the queue file, merged at the next start; the note says they are lost only if that side file cannot be written either) and the plugin keeps trying; a write that fails as EDMC closes can only go in EDMC's log |
| Queue file unreadable at start | if another program has it open, nothing is overwritten: the plugin reads it again before each save and adds its events back, and until then the EDMC line says the waiting events can't be saved. If it is damaged, it is kept beside the plugin as `queue.json.unreadable-<time>` and the EDMC line says so for 10 minutes |

From 0.6.6 the site also keeps the ids of refused events until they are applied (up to 1,000), so a retry is never skipped as already known, and it says why each skipped event was skipped; events it set aside because it already holds newer data are not counted as "already known". Resending is safe by design: from 0.6.5 the site remembers which events it applied (for about 48 hours) and
skips exactly those, so a batch sent twice is counted once, while an older event it never had (one it refused, or
one from a queue file read late) is still applied. That is why the safe move on any failure here is to keep the
batch rather than drop it. A newer event of a commander is never sent before an older one that is still waiting,
except an event the site refused: that one is set aside and retried (see above), so it holds nobody up. A batch the site stored none of is held, not set aside (0.6.7), except a lone event with nothing else queued, which is set aside after about an hour.

The site works out all calculations. The plugin does no arithmetic.

The site's owner can see, for each member, when they joined and were last seen, the plugin version and last check-in, how many batches and uploads arrived, and which pages they visit.

## Change history

0.6.8: odd site replies are checked (impossible counts keep the batch, site text is bounded), a destination id with control characters is refused, a stuck sender is shown on the EDMC line, the startup read can no longer overwrite a live commander, and the queue file carries a format version.
