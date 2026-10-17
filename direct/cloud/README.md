# Firebase cloud sketches

The application uses Firebase Authentication email/password accounts and the
existing Firebase Realtime Database. The application's built-in provider is
fixed to the `mcu-flasher-c46e3` deployment. The selector and Developer Access
offer no database, project or API-key editing controls. Environment variables,
legacy configuration files and saved vault provider values cannot select a
different deployment.
Passwords, tokens, service-account keys and developer unlock keys are never
loaded from the checkout. Firebase's web API key identifies a project; database
security comes from authenticated user isolation and deployed rules.

## Deployment administration and migration

1. In the Firebase project owned by this application, enable the **Email/Password**
   sign-in provider. The application already includes this project's public
   client configuration; users only sign in to their accounts.
2. Back up the existing database with Firebase's owner tools. Review the rules in
   `database.rules.json`, merge any unrelated existing application paths, and
   validate them in the Firebase Rules Simulator or Emulator before publishing.
   These rules deny anonymous access and access to another user's UID. They
   preserve both `/owner/{uid}/tickets` and all existing children under the
   account's `/users/{uid}` path, including legacy tickets; cloud sketches
   live at `/users/{uid}/cloud_sketches/{sketchId}`.
3. Publish reviewed rules only to the correct application database. Enabling
   Authentication alone does not enable sketch storage. `Permission denied`
   means the cloud endpoint was reached but this operation was denied by its
   rules; it does not mean the computer lacks internet access.
4. Restrict the public API key to the Firebase APIs this app needs, and review
   Authentication quotas for expected traffic. Never distribute an Admin SDK or
   service-account private key, or make database rules public as a workaround.
   Accounts and working copies from another provider are not migrated into this
   deployment automatically.
5. The client only operates on the authenticated account. Legacy ticket records
   remain at their existing paths; no automatic remote database rewrite runs at
   startup. Administrative account or data migration belongs in a private server
   tool, outside the application checkout.

No rules or API restrictions are automatically deployed by the desktop client.
Publishing requires administrative access to the correct Firebase project.
The rules enforce UID isolation, cloud field types and retained revision leaf
values. RTDB's rule language cannot count arbitrary children or sum source-byte
sizes; aggregate sketch/file/byte quotas are client limits. A production service
that needs abuse-resistant storage/billing quotas must additionally validate
aggregate writes through a private server and restrict direct client writes.

The Firebase web configuration is public client identification, not an
authorization secret. Encrypting it with a decryption key shipped in the same
desktop application cannot prevent extraction or modification by the machine's
owner. The supported app pins its provider, but database protection must remain
on the server: authenticated UID rules, restricted API usage and suitable abuse
controls. Firebase App Check requires a separately provisioned attestation flow;
it is not enabled by embedding this configuration. See Firebase's
[API-key security guidance](https://firebase.google.com/docs/projects/api-keys).

## Credentials on each host

Windows uses the current user's **Credential Manager** generic credentials.
Ubuntu uses the desktop **Secret Service** through `secret-tool` from
`libsecret-tools`, normally backed by GNOME Keyring. A missing or locked keyring
produces a visible error when an old saved session cannot be cleared or a new
credential cannot be stored. A temporary sign-in is allowed when this provider
has never saved credentials on this OS account. A small, empty marker outside
the installation records that encrypted credential storage has been used; it
contains no account, password or token. There is no plaintext or fixed-key
fallback. Credentials never travel with a sketch or a copied application folder.

**Save login** stores the email and literal password in the OS vault for filling
the login form. **Remember me** separately stores a refresh token for restoring
the authenticated session. Neither sends a token/password through process
arguments. Signing out removes the remembered session; **Forget saved login**
also removes the saved password. Without either option, tokens live only in the
current process. An independent cloud window may need another login when
Remember me is disabled. Passwords are sent only over verified HTTPS to Firebase;
the operating system protects saved values at rest. The client does not implement
its own encryption protocol, and machine owners retain access to their own OS
credential vault. Firebase web API keys identify the project and are not private
authentication secrets.

Cloud working sketches are stored with the generated board/library sources,
under `_MCUFlasherByNaph_src/Cloud/<sketch name>/`. The configured source folder
is shared with `Boards` and `Libs`; its default location is the current user's
Documents folder. Names are readable and safe on Windows and Ubuntu. Opening the
same verified provider/account/sketch reuses its linked saved working copy. If
that project is already open, its window is focused without pulling sources or
replacing editor buffers. This also applies to a verified open copy at an older
location: adoption waits until it closes and preserves its saved edits.
Existing duplicate folders are not cleaned automatically.

A same-name unlinked folder or another sketch's folder under the same account
requires **Replace** or **Cancel**. The prompt names the exact destination;
downloads never silently create numbered folders. A different account/provider's
linked folder is refused. Open target workspaces remain protected.

Private account state and pull recovery copies remain outside the installation:

- Windows: `%LOCALAPPDATA%/MCUFlasher/cloud/`
- Ubuntu: `$XDG_DATA_HOME/mcu-flasher/cloud/`, normally
  `~/.local/share/mcu-flasher/cloud/`

Account deletion requires the current password, deletes the account's cloud
sketches while its fresh token is authorized, then deletes the Firebase identity
and forgets its saved login/session. Local projects and local recovery copies
remain available. If identity deletion fails after sketch deletion, the client
reports that partial result and supports retry; Firebase Auth and RTDB do not
provide a single transaction across both services. Legacy developer tickets are
separate from the cloud sketch collection.

## Source boundaries and version behavior

Only `.ino`, `.cpp`, `.c`, `.h`, `.hpp` and `.txt` files directly inside the chosen
sketch folder are uploaded. Cache/journal folders, libraries, hidden files,
symbolic links and assets are excluded. Limits are 128 files, 1 MiB per file,
4 MiB of UTF-8 source/notes and 100 sketches per account. Filenames are checked
for both Windows and Ubuntu. At least one nonempty `.ino`, `.cpp` or `.c` primary
source is required. A cloud working copy includes a nonsecret
`.mcu_cloud_link.json` containing its UID, provider fingerprint, sketch ID,
tracked names and base revision. It contains no credential.
Uploading a local project creates a separate cloud sketch and leaves the local
folder unlinked. Open that cloud sketch to materialize its own per-user working
folder in a separate workspace.

The selected sketch's Actions menu offers Rename and Delete. Names are compared
without case or surrounding whitespace. Uploading an existing name requires an
explicit Replace or Save a copy choice; numbered **remote** copies use the first available
` (1)`, ` (2)`, etc. Replace preserves the sketch ID and appends a guarded source
revision. Rename rejects other sketches' names and appends a source-identical
revision to satisfy the existing published rules. Delete uses the sketch ETag
and selected revision, removes cloud history, and preserves all local sources,
links and recovery copies. These operations reject stale revisions or ETags;
they do not retry a conflicting write automatically. Existing working copies
keep their local revision after Rename/Replace, so explicitly Pull latest before
their next Push. These are bounded client name checks; independent simultaneous
clients do not provide a server-wide unique-name transaction.
These remote naming choices are separate from local working-folder collision
handling; opening/downloading does not create a numbered local copy.

Push reads source bytes and conditionally replaces the sketch record using the
Firebase ETag. A stale base revision or ETag fails with a conflict: explicitly
pull before pushing again. No automatic retry overwrites remote data. Up to
20 revisions and 16 MiB of serialized revision history are retained; oldest
revisions are removed when those bounds are reached.
Revision keys use `r1`, `r2`, etc. so Firebase's REST response retains an object
instead of automatically converting dense numeric keys into a JSON array.

Pull validates the complete snapshot and SHA-256 digests before replacing
anything. It preserves a local recovery copy outside the checkout, replaces
root source files through same-folder atomic replacements, and removes only
filenames already tracked by that sketch's valid link during an ordinary Pull.
An unknown conflicting local file, directory or symbolic link prevents that Pull.
An explicitly confirmed **Replace** of a local name collision additionally backs
up the reviewed root sources/notes and removes obsolete root source/note files.
Assets, settings, build caches and AI recovery journals remain intact.

The cloud revision, folder/link identity and affected source bytes are rechecked
after the prompt and staging, immediately before publication. Each file is
checked again before mutation; a newly open target also rejects replacement.
On failure, rollback restores only unchanged files owned by that attempt or
still-absent deleted paths. Concurrent external edits and links are retained,
and incomplete recovery reports the retained backup location. A redirected
project root is never followed for rollback or cleanup. Ubuntu fixtures cover
case-only filename changes and rollback spellings on its case-sensitive filesystem.
Cloud filesystem operations support extended Windows paths for deep project and
recovery folders. The extension is restricted to Windows I/O; visible project
paths, AI watcher paths and native Ubuntu paths retain their normal spelling.

Pulling an older revision restores that source locally while retaining the
latest cloud revision as the next push's comparison base. The next explicit
push creates a new revision; it never rewrites an old revision. Save pending
editor changes and confirm replacement before using Pull/Revert. All network
and source-storage work runs off the GUI thread. Opening the Cloud tab checks
reachability with a credential-free HTTPS `HEAD` request to the configured
Firebase database endpoint. It reads no database contents. This cloud-only check and Firebase
authentication/sync work independently of the package Offline Mode preference;
Offline Mode applies only to board/library preparation and never disables
application networking. Cloud and Developer Access each check Firebase directly.
All authenticated requests validate Firebase HTTPS destinations and reject
redirects so credentials cannot be forwarded to another host.

## Verification

Run the app's private Python with `-B direct/verify_cloud_sketch_service.py`.
The verifier supplies synthetic credentials, an in-memory HTTP database and
isolated `temp/` projects; it does not read a live vault, contact Firebase, deploy
rules, create/delete real accounts, modify a user sketch or access hardware.
Native Ubuntu Secret Service behavior must also be checked on an Ubuntu desktop
with an unlocked keyring. Mocked host checks do not prove native keyring access.

Also run `direct/verify_cloud_configuration.py`, `direct/verify_cloud_sketch_ui.py`
and `direct/verify_cloud_editor_manifest.py` with the private runtime. The Ubuntu
`direct/verify_ubuntu_cloud_workflow.py` fixture compares original/restored source
bytes, filenames, exact board/framework, configured library version, generated
INI and staged build inputs. It checks permissions and a long dependency path
with native host GCC; its ESP32 compiler is mocked. It neither installs a
toolchain nor certifies a full NimBLE firmware compilation. Use
`direct/verify_board_build_cache.py`, native runtime/Monaco Pull checks and compact
responsive fixtures for cache receipts, all editor models, pending AI edits,
independent windows and prompt layouts. All source/network/vault/hardware fixtures
remain isolated under `temp/`.

`direct/verify_cloud_rules.js` compiles and checks the rules against loopback Auth
and RTDB emulators, including REST history readback and twenty-revision retention.
Run it through Firebase `emulators:exec --only database,auth` using a synthetic
`demo-mcu-cloud-rules` project and an isolated `temp/` configuration. It requires
explicit loopback `FIREBASE_DATABASE_EMULATOR_HOST` and
`FIREBASE_AUTH_EMULATOR_HOST` values and refuses production hosts.

REST behavior follows the official
[Authentication REST API](https://firebase.google.com/docs/reference/rest/auth),
[conditional database requests](https://firebase.google.com/docs/database/rest/save-data#section-conditional-requests),
and [authenticated database rules](https://firebase.google.com/docs/database/security/rules-conditions).
