# EDMC-Engineering

An EDMarketConnector (EDMC) plugin for https://ed.golegend.com, an invite-only Elite Dangerous engineering planner. It sends your journal events to that site as the game writes them, so your ships, materials and progress there stay current without you re-uploading your journal folder.

Version 0.6.8 (`VERSION` in `load.py`).


## Installing it

The site is **https://ed.golegend.com**. You need an account there, and the plugin sends your
journal events to it.

1. **Install EDMC** (EDMarketConnector, current stable release from
   https://github.com/EDCD/EDMarketConnector/releases) if you do not have it.
2. **Get an account.** Registration needs an invite code from an existing member: they open
   **Account**, then **Invite someone**, and send you the code. A code works once and lasts 7
   days. On https://ed.golegend.com choose **Have an invite code? Create an account.** and enter it.
3. **Upload your journal folder** on the site once. The plugin adds live events on top of it.
4. **Get your plugin key.** On the site open **Account** and choose **Create key**. It is **shown
   once**, so copy it now.
5. **Download the plugin.** From https://github.com/majorxp00/EDMC-Engineering/releases download
   the newest `EDMC-Engineering-<version>.zip`.
6. **Put it in EDMC.** In EDMC: **File -> Settings -> Plugins -> Open**. Unzip the download into the
   folder that opens, so you get a folder `EDMC-Engineering` with `load.py` inside.
7. **Restart EDMC.**
8. **File -> Settings -> EDMC-Engineering**: paste your key into **Plugin key** and leave **Site** as it
   is. Close Settings.

**Is it working?** EDMC's main window gets one line, `Engineering:` with a status beside it, and it
changes only when you have something to do. `connected` means all is well and stays that way while
you play (no counts of events sent; the per-batch figures stay in the plugin's own state and are not shown). While the site is refusing some events and the plugin is holding them to try again, the line reads `connected, N event(s) the site could not store yet; retrying`. A second
line appears under it only to say something you should know, such as the queue file not saving. `upload your journal folder on the site first` means the site
has nothing stored for you yet (step 3). `can't reach the site, will keep trying` appears only after
several failed tries in a row; the plugin keeps the events and sends them when the site answers.

**Updating:** close EDMC with its window's X so it saves its settings (do not kill it from Task
Manager), download the newest release zip, replace `load.py` in the `EDMC-Engineering` folder with
the one inside it, and start EDMC again. The key and site are kept, and so is the queue file beside the plugin. If the Engineering line is missing afterwards,
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

## Change history

0.6.8: odd site replies are checked (impossible counts keep the batch, site text is bounded), a destination id with control characters is refused, a stuck sender is shown on the EDMC line, the startup read can no longer overwrite a live commander, and the queue file carries a format version.
