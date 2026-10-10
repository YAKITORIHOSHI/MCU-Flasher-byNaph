# Firebase cloud sketches

The application uses Firebase Authentication email/password accounts and the
existing Firebase Realtime Database. Client configuration is loaded only from
the current OS user's secure credential store. The selector never reads saved
connection values back into the UI; entry fields are masked, and settings stay
in the system vault rather than the application checkout or environment.
Passwords, tokens, service-account keys and developer unlock keys are never
loaded from the checkout. Firebase's web API key identifies a project; database
security comes from authenticated user isolation and deployed rules.

## Provider setup and migration

1. In the Firebase project owned by this application, enable the **Email/Password**
   sign-in provider. Use that project's web API key and HTTPS Realtime Database
   endpoint in the selector's Cloud configuration.
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
4. Save provider settings through `save_cloud_configuration` into the user's OS
   credential vault. Verify vault readback in isolated tests; never add settings
   to the source tree or use an embedded encryption seed. Rotate
   any private credentials formerly shipped in it. Never distribute an Admin
   SDK/service-account private key or make database rules public as a workaround.
5. The client only operates on the authenticated account. Legacy ticket records
   remain at their existing paths; no automatic remote database rewrite runs at
   startup. Administrative account or data migration belongs in a private server
   tool, outside the application checkout.

No rules or provider settings are automatically deployed by the desktop client.
Publishing requires administrative access to the correct Firebase project.
The rules enforce UID isolation, cloud field types and retained revision leaf
values. RTDB's rule language cannot count arbitrary children or sum source-byte
sizes; aggregate sketch/file/byte quotas are client limits. A production service
that needs abuse-resistant storage/billing quotas must additionally validate
aggregate writes through a private server and restrict direct client writes.

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

User cloud projects and pull recovery copies are outside the installation:

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
filenames already tracked by that sketch's valid link. An unknown local file,
directory or symbolic link prevents the pull. Protected build caches and AI
recovery journals are never touched. On a storage failure, the previous linked
files are restored when possible and the recovery folder is retained.
The folder identity, cloud link and touched source bytes are checked again after
staging; an external edit during preparation rejects the pull before replacement.
Cloud filesystem operations support extended Windows paths for deep project and
recovery folders. The extension is restricted to Windows I/O; visible project
paths, AI watcher paths and native Ubuntu paths retain their normal spelling.

Pulling an older revision restores that source locally while retaining the
latest cloud revision as the next push's comparison base. The next explicit
push creates a new revision; it never rewrites an old revision. Save pending
editor changes and confirm replacement before using Pull/Revert. All network
and source-storage work runs off the GUI thread. Opening the Cloud tab checks
reachability with a credential-free HTTPS `HEAD` request to the configured
Firebase database endpoint, or Firebase Authentication when no database is
configured. It reads no database contents. This cloud-only check and Firebase
authentication/sync work independently of the package Offline Mode preference;
Offline Mode still blocks other application network activity. While its audit
guard is active, only scoped HTTPS requests to Firebase Auth and Realtime Database
hosts pass through.

## Verification

Run the app's private Python with `-B direct/verify_cloud_sketch_service.py`.
The verifier supplies synthetic credentials, an in-memory HTTP database and
isolated `temp/` projects; it does not read a live vault, contact Firebase, deploy
rules, create/delete real accounts, modify a user sketch or access hardware.
Native Ubuntu Secret Service behavior must also be checked on an Ubuntu desktop
with an unlocked keyring. Mocked host checks do not prove native keyring access.
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
