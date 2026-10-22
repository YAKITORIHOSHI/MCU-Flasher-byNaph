//! Disposable trees only. No application GUI, cache, account or hardware access.
use mcu_contracts::{
    ContentDigest, ErrorCode, Revision, SaveDocumentRequest, ScaffoldOptions, ScaffoldTemplate,
};
use mcu_core::{sanitized_project_name, source_digest, ProjectLimits, ProjectService};
use std::fs;
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicU64, Ordering};

static NEXT_FIXTURE: AtomicU64 = AtomicU64::new(1);

struct Fixture {
    root: PathBuf,
    application: PathBuf,
    sandbox: PathBuf,
    project: PathBuf,
}

impl Fixture {
    fn new() -> Self {
        let checkout = Path::new(env!("CARGO_MANIFEST_DIR"))
            .ancestors()
            .nth(3)
            .unwrap();
        let root = checkout.join("audit/migration/scratch").join(format!(
            "core-{}-{}",
            std::process::id(),
            NEXT_FIXTURE.fetch_add(1, Ordering::Relaxed)
        ));
        fs::create_dir_all(&root).unwrap();
        let application = root.join("app");
        let sandbox = application.join("audit/migration/scratch");
        let project = sandbox.join("Demo");
        fs::create_dir_all(&project).unwrap();
        fs::write(
            project.join("Demo.ino"),
            b"// baseline\r\nvoid setup() {}\r\n",
        )
        .unwrap();
        Self {
            root,
            application,
            sandbox,
            project,
        }
    }

    fn service(&self) -> ProjectService {
        ProjectService::fixture(&self.application, &self.sandbox).unwrap()
    }

    fn empty_options(name: &str) -> ScaffoldOptions {
        ScaffoldOptions {
            name: name.into(),
            include_h: false,
            include_cpp: false,
            template: ScaffoldTemplate::Standard,
        }
    }
}

impl Drop for Fixture {
    fn drop(&mut self) {
        // This exact unique test tree was created above. Never operate on a
        // caller-supplied root or application metadata during test cleanup.
        fs::remove_dir_all(&self.root).unwrap();
    }
}

fn save_request(snapshot: &mcu_contracts::DocumentSnapshot, content: &str) -> SaveDocumentRequest {
    SaveDocumentRequest {
        workspace_id: snapshot.workspace_id.clone(),
        document_id: snapshot.document.document_id.clone(),
        expected_revision: snapshot.document.revision,
        expected_digest: snapshot.document.content_digest.clone(),
        edit_revision: Revision(7),
        content: content.into(),
    }
}

fn native_scaffold_text(text: &str) -> String {
    if cfg!(windows) {
        text.replace('\n', "\r\n")
    } else {
        text.into()
    }
}

#[test]
fn root_manifest_preserves_order_and_never_traverses_nested_cache_ide_or_sources() {
    let fixture = Fixture::new();
    fs::write(fixture.project.join("z.INO"), "// second sketch\n").unwrap();
    fs::write(fixture.project.join("a.hpp"), "#pragma once\n").unwrap();
    fs::write(fixture.project.join("Note.txt"), "literal note\n").unwrap();
    fs::write(fixture.project.join("image.bin"), [0xff, 0x00]).unwrap();
    fs::write(fixture.project.join(".hidden.txt"), "ignored\n").unwrap();
    for name in [
        ".mcu_flasher_build_cache",
        ".pio",
        ".vscode",
        ".clangd",
        ".opencode",
        "src",
    ] {
        fs::create_dir(fixture.project.join(name)).unwrap();
        fs::write(
            fixture.project.join(name).join("NeverRead.ino"),
            [0xff; 4096],
        )
        .unwrap();
    }
    let mut service = fixture.service();
    let registration = service.register_project("main", &fixture.project).unwrap();
    let names: Vec<_> = registration
        .manifest
        .files
        .iter()
        .map(|file| file.name.as_str())
        .collect();
    assert_eq!(names, ["Demo.ino", "z.INO", "a.hpp", "Note.txt"]);
    let same = service
        .manifest("main", &registration.workspace_id)
        .unwrap();
    assert_eq!(same, registration.manifest);
    assert_eq!(
        fs::read(
            fixture
                .project
                .join(".mcu_flasher_build_cache/NeverRead.ino")
        )
        .unwrap(),
        [0xff; 4096]
    );
}

#[test]
fn byte_exact_newline_and_bom_read_and_unchanged_save_do_not_rewrite() {
    let fixture = Fixture::new();
    let bytes = b"\xef\xbb\xbf// UTF-8 caf\xc3\xa9\r\nvoid setup() {}\n";
    fs::write(fixture.project.join("Demo.ino"), bytes).unwrap();
    let before = fs::metadata(fixture.project.join("Demo.ino"))
        .unwrap()
        .modified()
        .unwrap();
    let mut service = fixture.service();
    let registration = service.register_project("main", &fixture.project).unwrap();
    let file = &registration.manifest.files[0];
    let snapshot = service
        .read_document("main", &registration.workspace_id, &file.document_id)
        .unwrap();
    assert_eq!(snapshot.content.as_bytes(), bytes);
    assert_eq!(snapshot.document.content_digest, source_digest(bytes));
    let receipt = service
        .save_document("main", save_request(&snapshot, &snapshot.content))
        .unwrap();
    assert!(receipt.unchanged);
    assert_eq!(receipt.bytes_written, 0);
    assert_eq!(receipt.revision, snapshot.document.revision);
    assert_eq!(receipt.edit_revision, Revision(7));
    assert_eq!(
        before,
        fs::metadata(fixture.project.join("Demo.ino"))
            .unwrap()
            .modified()
            .unwrap()
    );
    assert_eq!(fs::read_dir(&fixture.project).unwrap().count(), 1);
}

#[test]
fn changed_save_acknowledges_actual_digest_and_rejects_old_revision() {
    let fixture = Fixture::new();
    let mut service = fixture.service();
    let registration = service.register_project("main", &fixture.project).unwrap();
    let snapshot = service
        .read_document(
            "main",
            &registration.workspace_id,
            &registration.manifest.files[0].document_id,
        )
        .unwrap();
    let saved = "// hello \u{1f680}\r\nvoid setup() {}\r\n";
    let receipt = service
        .save_document("main", save_request(&snapshot, saved))
        .unwrap();
    assert!(!receipt.unchanged);
    assert_eq!(receipt.bytes_written, saved.len() as u64);
    assert_eq!(receipt.content_digest, source_digest(saved.as_bytes()));
    assert_eq!(receipt.revision, Revision(snapshot.document.revision.0 + 1));
    assert_eq!(
        fs::read(fixture.project.join("Demo.ino")).unwrap(),
        saved.as_bytes()
    );
    let stale = service
        .save_document("main", save_request(&snapshot, "// stale\n"))
        .unwrap_err();
    assert_eq!(stale.code, ErrorCode::StaleRevision);
    assert_eq!(fs::read_dir(&fixture.project).unwrap().count(), 1);
}

#[test]
fn external_same_length_change_rejected_and_manifest_advances_from_actual_bytes() {
    let fixture = Fixture::new();
    let mut service = fixture.service();
    let registration = service.register_project("main", &fixture.project).unwrap();
    let file = &registration.manifest.files[0];
    let snapshot = service
        .read_document("main", &registration.workspace_id, &file.document_id)
        .unwrap();
    let external = snapshot.content.replace("baseline", "external");
    assert_eq!(external.len(), snapshot.content.len());
    fs::write(fixture.project.join("Demo.ino"), &external).unwrap();
    let failure = service
        .save_document("main", save_request(&snapshot, "// my dirty buffer\n"))
        .unwrap_err();
    assert_eq!(failure.code, ErrorCode::ExternalChange);
    assert_eq!(
        fs::read_to_string(fixture.project.join("Demo.ino")).unwrap(),
        external
    );
    let manifest = service
        .manifest("main", &registration.workspace_id)
        .unwrap();
    assert!(manifest.revision > registration.manifest.revision);
    assert_eq!(manifest.files[0].revision, Revision(file.revision.0 + 1));
    assert_eq!(
        manifest.files[0].content_digest,
        source_digest(external.as_bytes())
    );
    assert_eq!(manifest.files[0].document_id, file.document_id);
    let stale = service
        .save_document("main", save_request(&snapshot, "// old snapshot\n"))
        .unwrap_err();
    assert_eq!(stale.code, ErrorCode::StaleRevision);
}

#[test]
fn refreshed_read_updates_revision_but_missing_source_is_never_reported_as_content() {
    let fixture = Fixture::new();
    let mut service = fixture.service();
    let registration = service.register_project("main", &fixture.project).unwrap();
    let file = &registration.manifest.files[0];
    fs::write(fixture.project.join("Demo.ino"), "// external refreshed\n").unwrap();
    let refreshed = service
        .read_document("main", &registration.workspace_id, &file.document_id)
        .unwrap();
    assert!(refreshed.document.revision > file.revision);
    fs::remove_file(fixture.project.join("Demo.ino")).unwrap();
    let missing = service
        .read_document("main", &registration.workspace_id, &file.document_id)
        .unwrap_err();
    assert_eq!(missing.code, ErrorCode::NotFound);
    let save_missing = service
        .save_document("main", save_request(&refreshed, "// never recreate\n"))
        .unwrap_err();
    assert_eq!(save_missing.code, ErrorCode::NotFound);
    assert!(!fixture.project.join("Demo.ino").exists());
    let manifest = service
        .manifest("main", &registration.workspace_id)
        .unwrap();
    assert!(manifest.files.is_empty());
    assert!(manifest.revision > registration.manifest.revision);
    fs::remove_dir(&fixture.project).unwrap();
    assert_eq!(
        service
            .manifest("main", &registration.workspace_id)
            .unwrap_err()
            .code,
        ErrorCode::NotFound
    );
}

#[test]
fn invalid_utf8_fails_without_replacement_characters_or_disk_mutation() {
    let fixture = Fixture::new();
    let bytes = [0xff, 0x00, 0x80];
    fs::write(fixture.project.join("Demo.ino"), bytes).unwrap();
    let mut service = fixture.service();
    let registration = service.register_project("main", &fixture.project).unwrap();
    let error = service
        .read_document(
            "main",
            &registration.workspace_id,
            &registration.manifest.files[0].document_id,
        )
        .unwrap_err();
    assert_eq!(error.code, ErrorCode::InvalidEncoding);
    assert_eq!(fs::read(fixture.project.join("Demo.ino")).unwrap(), bytes);
}

#[test]
fn workspace_and_document_authority_cannot_be_forged_between_windows_or_roots() {
    let fixture = Fixture::new();
    let other = fixture.sandbox.join("Other");
    fs::create_dir(&other).unwrap();
    fs::write(other.join("Other.ino"), "// other\n").unwrap();
    let mut service = fixture.service();
    let registration = service.register_project("one", &fixture.project).unwrap();
    let second = service.register_project("two", &other).unwrap();
    assert_eq!(
        service
            .manifest("two", &registration.workspace_id)
            .unwrap_err()
            .code,
        ErrorCode::WrongOwner
    );
    assert_eq!(
        service
            .register_project("two", &fixture.project)
            .unwrap_err()
            .code,
        ErrorCode::WorkspaceBusy
    );
    assert_eq!(
        service
            .read_document(
                "one",
                &registration.workspace_id,
                &second.manifest.files[0].document_id
            )
            .unwrap_err()
            .code,
        ErrorCode::NotFound
    );
    assert_eq!(
        service
            .register_project("one", &fixture.project)
            .unwrap()
            .workspace_id,
        registration.workspace_id
    );
    service
        .unregister_project("one", &registration.workspace_id)
        .unwrap();
    assert_eq!(
        service
            .manifest("one", &registration.workspace_id)
            .unwrap_err()
            .code,
        ErrorCode::WorkspaceNotFound
    );
    let reopened = service.register_project("two", &fixture.project).unwrap();
    assert_ne!(reopened.workspace_id, registration.workspace_id);
    assert_ne!(
        reopened.manifest.files[0].document_id,
        registration.manifest.files[0].document_id
    );
}

#[test]
fn production_refuses_application_descendants_and_fixture_scope_is_explicit() {
    let fixture = Fixture::new();
    let mut production = ProjectService::new(&fixture.application).unwrap();
    assert_eq!(
        production
            .register_project("main", &fixture.project)
            .unwrap_err()
            .code,
        ErrorCode::ForbiddenRoot
    );
    assert_eq!(
        ProjectService::fixture(&fixture.application, &fixture.root)
            .err()
            .unwrap()
            .code,
        ErrorCode::ForbiddenRoot
    );
    let unrelated = fixture.root.join("user-sketch");
    fs::create_dir(&unrelated).unwrap();
    fs::write(unrelated.join("Sketch.ino"), "// selected\n").unwrap();
    assert!(production.register_project("main", &unrelated).is_ok());
    assert_eq!(
        fixture
            .service()
            .register_project("main", &unrelated)
            .unwrap_err()
            .code,
        ErrorCode::ForbiddenRoot
    );
    assert_eq!(
        production
            .register_project("main", Path::new("."))
            .unwrap_err()
            .code,
        ErrorCode::InvalidPath
    );
    let mut clone = fixture.root.join("cloned-app");
    fs::create_dir_all(clone.join("main")).unwrap();
    fs::write(clone.join("main/web_bridge.py"), "# signature\n").unwrap();
    assert_eq!(
        production
            .register_project("main", &clone)
            .unwrap_err()
            .code,
        ErrorCode::ForbiddenRoot
    );
    clone.push("..");
    assert_eq!(
        production
            .register_project("main", &clone)
            .unwrap_err()
            .code,
        ErrorCode::InvalidPath
    );
}

#[test]
fn per_file_aggregate_entry_and_source_limits_fail_as_structured_errors() {
    let fixture = Fixture::new();
    let external = fixture.root.join("bounded");
    fs::create_dir(&external).unwrap();
    fs::write(external.join("A.ino"), "12345").unwrap();
    let limits = ProjectLimits {
        max_source_bytes: 4,
        max_manifest_bytes: 8,
        ..ProjectLimits::default()
    };
    let mut service = ProjectService::with_limits(&fixture.application, limits).unwrap();
    assert_eq!(
        service
            .register_project("main", &external)
            .unwrap_err()
            .code,
        ErrorCode::TooLarge
    );
    fs::write(external.join("A.ino"), "1234").unwrap();
    fs::write(external.join("B.cpp"), "1234").unwrap();
    fs::write(external.join("C.h"), "1").unwrap();
    assert_eq!(
        service
            .register_project("main", &external)
            .unwrap_err()
            .code,
        ErrorCode::TooLarge
    );
    fs::remove_file(external.join("C.h")).unwrap();
    let registration = service.register_project("main", &external).unwrap();
    let snapshot = service
        .read_document(
            "main",
            &registration.workspace_id,
            &registration.manifest.files[0].document_id,
        )
        .unwrap();
    assert_eq!(
        service
            .save_document("main", save_request(&snapshot, "12345"))
            .unwrap_err()
            .code,
        ErrorCode::TooLarge
    );
    let count_limits = ProjectLimits {
        max_sources: 1,
        ..ProjectLimits::default()
    };
    assert_eq!(
        ProjectService::with_limits(&fixture.application, count_limits)
            .unwrap()
            .register_project("main", &external)
            .unwrap_err()
            .code,
        ErrorCode::LimitExceeded
    );
    let entry_limits = ProjectLimits {
        max_root_entries: 1,
        ..ProjectLimits::default()
    };
    assert_eq!(
        ProjectService::with_limits(&fixture.application, entry_limits)
            .unwrap()
            .register_project("main", &external)
            .unwrap_err()
            .code,
        ErrorCode::LimitExceeded
    );
}

#[test]
fn changed_digest_and_unknown_document_do_not_write_anything() {
    let fixture = Fixture::new();
    let mut service = fixture.service();
    let registration = service.register_project("main", &fixture.project).unwrap();
    let snapshot = service
        .read_document(
            "main",
            &registration.workspace_id,
            &registration.manifest.files[0].document_id,
        )
        .unwrap();
    let mut request = save_request(&snapshot, "// bad digest\n");
    request.expected_digest = ContentDigest("0".repeat(64));
    assert_eq!(
        service.save_document("main", request).unwrap_err().code,
        ErrorCode::ExternalChange
    );
    let mut request = save_request(&snapshot, "// fake name\n");
    request.document_id.0 = "../outside.cpp".into();
    assert_eq!(
        service.save_document("main", request).unwrap_err().code,
        ErrorCode::NotFound
    );
    assert_eq!(
        fs::read_to_string(fixture.project.join("Demo.ino")).unwrap(),
        snapshot.content
    );
}

#[test]
fn selected_header_is_included_in_every_generated_sketch_and_optional_cpp() {
    let fixture = Fixture::new();
    let mut service = fixture.service();
    let options = ScaffoldOptions {
        name: " With spaces/caf\u{00e9} ".into(),
        include_h: true,
        include_cpp: true,
        template: ScaffoldTemplate::Standard,
    };
    let created = service
        .create_project("main", &fixture.sandbox, options)
        .unwrap();
    assert_eq!(created.name, "With_spaces_caf_");
    let project = PathBuf::from(created.root);
    let include = "#include \"With_spaces_caf_.h\"\n";
    assert_eq!(
        fs::read_to_string(project.join("With_spaces_caf_.h")).unwrap(),
        native_scaffold_text("#pragma once\n")
    );
    assert_eq!(
        fs::read_to_string(project.join("With_spaces_caf_.cpp")).unwrap(),
        native_scaffold_text(include)
    );
    assert_eq!(
        fs::read_to_string(project.join("With_spaces_caf_.ino")).unwrap(),
        native_scaffold_text(&format!(
            "{include}\nvoid setup() {{\n\n}}\n\nvoid loop() {{\n\n}}\n"
        ))
    );
    assert_eq!(created.manifest.files.len(), 3);
    assert_eq!(sanitized_project_name("  "), "NewSketch");
}

#[test]
fn scaffolds_match_standard_bare_and_blink_reference_and_never_adopt_existing_folder() {
    let fixture = Fixture::new();
    let mut service = fixture.service();
    let mut standard = Fixture::empty_options("Plain");
    standard.include_cpp = true;
    let created = service
        .create_project("main", &fixture.sandbox, standard.clone())
        .unwrap();
    let path = PathBuf::from(created.root);
    assert_eq!(fs::read(path.join("Plain.cpp")).unwrap(), b"");
    assert_eq!(
        fs::read_to_string(path.join("Plain.ino")).unwrap(),
        native_scaffold_text("void setup() {\n\n}\n\nvoid loop() {\n\n}\n")
    );
    assert_eq!(
        service
            .create_project("main", &fixture.sandbox, standard)
            .unwrap_err()
            .code,
        ErrorCode::AlreadyExists
    );
    assert_eq!(
        fs::read_to_string(path.join("Plain.ino")).unwrap(),
        native_scaffold_text("void setup() {\n\n}\n\nvoid loop() {\n\n}\n")
    );
    let mut bare = Fixture::empty_options("Bare");
    bare.template = ScaffoldTemplate::Bare;
    let bare = service
        .create_project("main", &fixture.sandbox, bare)
        .unwrap();
    assert_eq!(
        fs::read_to_string(Path::new(&bare.root).join("Bare.ino")).unwrap(),
        native_scaffold_text("void setup() {\n  \n}\n\nvoid loop() {\n  \n}\n")
    );
    let mut blink = Fixture::empty_options("Blink");
    blink.template = ScaffoldTemplate::Blink;
    let blink = service
        .create_project("main", &fixture.sandbox, blink)
        .unwrap();
    assert_eq!(fs::read_to_string(Path::new(&blink.root).join("Blink.ino")).unwrap(), native_scaffold_text("void setup() {\n  pinMode(LED_BUILTIN, OUTPUT);\n}\n\nvoid loop() {\n  digitalWrite(LED_BUILTIN, HIGH);\n  delay(1000);\n  digitalWrite(LED_BUILTIN, LOW);\n  delay(1000);\n}\n"));
    assert_eq!(
        service
            .create_project("main", &fixture.sandbox, Fixture::empty_options("CON"))
            .unwrap_err()
            .code,
        ErrorCode::InvalidPath
    );
    assert!(!fixture.sandbox.join("CON").exists());
}

#[cfg(unix)]
#[test]
fn symlink_sources_roots_and_retargeted_paths_are_rejected_without_following() {
    use std::os::unix::fs::symlink;
    let fixture = Fixture::new();
    let outside = fixture.root.join("outside.cpp");
    fs::write(&outside, "// outside\n").unwrap();
    let mut service = fixture.service();
    let registration = service.register_project("main", &fixture.project).unwrap();
    let file = &registration.manifest.files[0];
    fs::remove_file(fixture.project.join("Demo.ino")).unwrap();
    symlink(&outside, fixture.project.join("Demo.ino")).unwrap();
    assert_eq!(
        service
            .read_document("main", &registration.workspace_id, &file.document_id)
            .unwrap_err()
            .code,
        ErrorCode::Symlink
    );
    assert_eq!(
        service
            .manifest("main", &registration.workspace_id)
            .unwrap_err()
            .code,
        ErrorCode::Symlink
    );
    assert_eq!(fs::read_to_string(&outside).unwrap(), "// outside\n");
    let alias = fixture.sandbox.join("Alias");
    symlink(&fixture.project, &alias).unwrap();
    assert_eq!(
        service.register_project("main", &alias).unwrap_err().code,
        ErrorCode::Symlink
    );
}

#[cfg(unix)]
#[test]
fn read_only_save_is_refused_and_unchanged_save_never_changes_permissions() {
    use std::os::unix::fs::PermissionsExt;
    let fixture = Fixture::new();
    let path = fixture.project.join("Demo.ino");
    fs::set_permissions(&path, fs::Permissions::from_mode(0o444)).unwrap();
    let mut service = fixture.service();
    let registration = service.register_project("main", &fixture.project).unwrap();
    let snapshot = service
        .read_document(
            "main",
            &registration.workspace_id,
            &registration.manifest.files[0].document_id,
        )
        .unwrap();
    assert!(
        service
            .save_document("main", save_request(&snapshot, &snapshot.content))
            .unwrap()
            .unchanged
    );
    assert_eq!(
        service
            .save_document("main", save_request(&snapshot, "// do not chmod\n"))
            .unwrap_err()
            .code,
        ErrorCode::ReadOnly
    );
    assert_eq!(
        fs::metadata(&path).unwrap().permissions().mode() & 0o777,
        0o444
    );
    assert_eq!(fs::read_to_string(&path).unwrap(), snapshot.content);
    assert_eq!(fs::read_dir(&fixture.project).unwrap().count(), 1);
}

#[cfg(unix)]
#[test]
fn portable_case_and_device_aliases_are_rejected_before_registration() {
    use std::io::{ErrorKind, Write};
    use std::os::unix::fs::MetadataExt;

    let fixture = Fixture::new();
    let original = fixture.project.join("Demo.ino");
    let baseline = fs::read(&original).unwrap();
    let original_metadata = fs::metadata(&original).unwrap();
    let alternate_case = fixture.project.join("demo.INO");
    match fs::OpenOptions::new()
        .write(true)
        .create_new(true)
        .open(&alternate_case)
    {
        Ok(mut conflict) => {
            conflict.write_all(b"// conflict\n").unwrap();
            drop(conflict);
            assert_eq!(fs::read_dir(&fixture.project).unwrap().count(), 2);
            assert_ne!(
                fs::metadata(&alternate_case).unwrap().ino(),
                original_metadata.ino()
            );
            assert_eq!(
                fixture
                    .service()
                    .register_project("main", &fixture.project)
                    .unwrap_err()
                    .code,
                ErrorCode::InvalidPath
            );
            fs::remove_file(&alternate_case).unwrap();
        }
        Err(error) if error.kind() == ErrorKind::AlreadyExists => {
            // Case-insensitive volumes prevent a second entry. Prove that the
            // failed create preserved the original instead of truncating it.
            let alias_metadata = fs::metadata(&alternate_case).unwrap();
            assert_eq!(alias_metadata.dev(), original_metadata.dev());
            assert_eq!(alias_metadata.ino(), original_metadata.ino());
            assert_eq!(fs::read(&alternate_case).unwrap(), baseline);
            assert_eq!(fs::read_dir(&fixture.project).unwrap().count(), 1);
            let registration = fixture
                .service()
                .register_project("main", &fixture.project)
                .unwrap();
            assert_eq!(registration.manifest.files.len(), 1);
            assert_eq!(registration.manifest.files[0].name, "Demo.ino");
            assert_eq!(
                registration.manifest.files[0].content_digest,
                source_digest(&baseline)
            );
        }
        Err(error) => panic!("Cannot create the case-collision fixture: {error}"),
    }
    assert_eq!(fs::read(&original).unwrap(), baseline);
    fs::write(fixture.project.join("NUL.txt"), "// device\n").unwrap();
    assert_eq!(
        fixture
            .service()
            .register_project("main", &fixture.project)
            .unwrap_err()
            .code,
        ErrorCode::InvalidPath
    );
    fs::remove_file(fixture.project.join("NUL.txt")).unwrap();
    fs::write(fixture.project.join("alternate:stream.cpp"), "// ADS\n").unwrap();
    assert_eq!(
        fixture
            .service()
            .register_project("main", &fixture.project)
            .unwrap_err()
            .code,
        ErrorCode::InvalidPath
    );
}
