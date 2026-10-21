//! Explicit development preparation of the four acquired Linux AVR archives.
//! No process, installation, live package-store or firmware action is exposed.

use mcu_host::prepare_avr_fixture::{
    prepare_avr_fixture, prepare_avr_package_fixture, AvrArchiveHost, AvrArchiveInput,
    AvrArchiveRole, AvrFixtureHost,
};
use sha2::{Digest, Sha256};
use std::fs::{self, File};
use std::io::{Read, Seek, SeekFrom};
use std::path::Path;
use std::time::{SystemTime, UNIX_EPOCH};

fn main() {
    let arguments = std::env::args_os().skip(1).collect::<Vec<_>>();
    let packages = if arguments == ["--prepare-linux-x86_64"] {
        false
    } else if arguments == ["--prepare-packages-linux-x86_64"] {
        true
    } else {
        eprintln!("Use --prepare-linux-x86_64 or --prepare-packages-linux-x86_64; all paths and identities are fixed.");
        std::process::exit(125);
    };
    match prepare(packages) {
        Ok(receipt) => println!("{receipt}"),
        Err(error) => {
            eprintln!("{error}");
            std::process::exit(1);
        }
    }
}

fn prepare(packages: bool) -> Result<serde_json::Value, String> {
    if (std::env::consts::OS, std::env::consts::ARCH) != ("linux", "x86_64") {
        return Err("The acquired payload qualification is Linux x86_64 only.".into());
    }
    let application = Path::new(env!("CARGO_MANIFEST_DIR"))
        .join("../../..")
        .canonicalize()
        .map_err(|error| error.to_string())?;
    let parent = application.join("temp/audit/native-compile-inputs");
    let input = parent.join("avr-archives");
    // These are exact anonymous registry records retained with the acquisition
    // receipt. Metadata fetched at runtime cannot replace a native constant.
    let archives = [
        (
            AvrArchiveRole::Platform,
            "atmelavr-5.3.0.tar.gz",
            41_655,
            "370a482b8cd980b8954c03cec3e856f50758cac695491e6b6c8327f5e60079ac",
        ),
        (
            AvrArchiveRole::Framework,
            "framework-arduino-avr-5.4.0.tar.gz",
            565_720,
            "bf85bcca114bad389fec51fecbf9b66821a233b366bd3f85cdb1cdcba6a28659",
        ),
        (
            AvrArchiveRole::ToolScons,
            "tool-scons-4.41101.0.tar.gz",
            523_448,
            "e405de1fcd3307fa80d624f0290c7cd30fbb47f67e135a9f56d7e46b6b895311",
        ),
        (
            AvrArchiveRole::Toolchain,
            "toolchain-atmelavr-linux_x86_64-1.70300.191015.tar.gz",
            52_965_047,
            "664f0b7b08a15e8d6362b87e2fdb5a4dcfd5959ebc5b19d938e7dff6f12f0524",
        ),
    ]
    .map(|(role, name, size, digest)| AvrArchiveInput {
        role,
        archive_path: input.join(name),
        expected_sha256: digest.into(),
        expected_size: size,
        host: if role == AvrArchiveRole::Toolchain {
            AvrArchiveHost::Native(AvrFixtureHost::LinuxX86_64)
        } else {
            AvrArchiveHost::Universal
        },
    });
    let timestamp = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map_err(|error| error.to_string())?
        .as_nanos();
    let label = format!("prepared-avr-linux-{}-{timestamp}", std::process::id());
    let prepare_fixture = if packages {
        prepare_avr_package_fixture
    } else {
        prepare_avr_fixture
    };
    let prepared = prepare_fixture(
        &application,
        &parent,
        &label,
        AvrFixtureHost::LinuxX86_64,
        archives,
    )
    .map_err(|error| {
        format!("Owned preparation refused: {error:?}; any partial reservation is retained.")
    })?;

    let mut files = prepared.files().iter().collect::<Vec<_>>();
    files.sort_unstable_by(|left, right| left.path.cmp(&right.path));
    let mut total = 0u64;
    let mut aggregate = Sha256::new();
    let mut archive_aggregate = Sha256::new();
    for file in files {
        let path = prepared.root().join(&file.path);
        let metadata = fs::symlink_metadata(&path).map_err(|error| error.to_string())?;
        if !metadata.is_file() || metadata.len() != file.bytes {
            return Err(format!(
                "Prepared file changed before probe verification: {}",
                file.path
            ));
        }
        #[cfg(unix)]
        {
            use std::os::unix::fs::{MetadataExt, PermissionsExt};
            if metadata.nlink() != 1 || metadata.permissions().mode() & 0o7777 != file.unix_mode {
                return Err(format!(
                    "Prepared mode or link identity changed: {}",
                    file.path
                ));
            }
        }
        let (digest, bytes) = hash_file(&path)?;
        if digest != file.sha256 || bytes != file.bytes {
            return Err(format!(
                "Prepared content changed before probe verification: {}",
                file.path
            ));
        }
        total = total
            .checked_add(bytes)
            .ok_or("Prepared byte total overflowed")?;
        let row = format!(
            "{}\0{}\0{}\0{:o}\n",
            file.path, bytes, digest, file.unix_mode
        );
        aggregate.update(&row);
        if !prepared
            .generated_metadata()
            .iter()
            .any(|record| record.path == file.path)
        {
            archive_aggregate.update(&row);
        }
    }
    let archive_hash = format!("{:x}", archive_aggregate.finalize());
    if archive_hash != "5afff446889ffa0fbad05024be11f1225eee97299e6ddfc1d2d4341bc40c4631" {
        return Err(
            "Original prepared package files differ from the verified archive-only checkpoint."
                .into(),
        );
    }
    let (expected_files, expected_bytes) = if packages {
        (2762, 240_450_165)
    } else {
        (2758, 240_449_534)
    };
    if prepared.files().len() != expected_files || total != expected_bytes {
        return Err(
            "Exact acquired archive file/byte totals did not match the reviewed layout.".into(),
        );
    }
    let expected_metadata = [
        (
            "core/platforms/atmelavr/.piopm",
            "atmelavr",
            "5.3.0",
            "platform",
            8006,
        ),
        (
            "core/packages/framework-arduino-avr/.piopm",
            "framework-arduino-avr",
            "5.4.0",
            "tool",
            8039,
        ),
        (
            "core/packages/tool-scons/.piopm",
            "tool-scons",
            "4.41101.0",
            "tool",
            8192,
        ),
        (
            "core/packages/toolchain-atmelavr/.piopm",
            "toolchain-atmelavr",
            "1.70300.191015",
            "tool",
            8203,
        ),
    ];
    if prepared.generated_metadata().len() != if packages { 4 } else { 0 } {
        return Err("The native metadata manifest has the wrong record count.".into());
    }
    for (record, (path, name, version, kind, id)) in
        prepared.generated_metadata().iter().zip(expected_metadata)
    {
        if record.path != path || record.registry_id != id || record.bytes > 512 {
            return Err(
                "Native package metadata differs from the reviewed registry identity.".into(),
            );
        }
        let bytes = fs::read(prepared.root().join(path)).map_err(|error| error.to_string())?;
        let value: serde_json::Value =
            serde_json::from_slice(&bytes).map_err(|error| error.to_string())?;
        if value
            != serde_json::json!({"type":kind,"name":name,"version":version,"spec":{"owner":"platformio","id":id,"name":name,"requirements":null,"uri":null}})
        {
            return Err(
                "Native package lookup metadata differs from PlatformIO's verified format.".into(),
            );
        }
    }
    let compiler = prepared
        .root()
        .join("core/packages/toolchain-atmelavr/bin/avr-gcc");
    let mut handle = File::open(&compiler).map_err(|error| error.to_string())?;
    let mut header = [0u8; 64];
    handle
        .read_exact(&mut header)
        .map_err(|error| error.to_string())?;
    if header[..7] != *b"\x7fELF\x02\x01\x01"
        || u16::from_le_bytes([header[16], header[17]]) != 2
        || u16::from_le_bytes([header[18], header[19]]) != 62
    {
        return Err(
            "The prepared compiler is not the reviewed ELF64 little-endian x86_64 image.".into(),
        );
    }
    handle
        .seek(SeekFrom::Start(0))
        .map_err(|error| error.to_string())?;
    let (compiler_hash, compiler_bytes) = hash_reader(&mut handle)?;
    if compiler_hash != "8b9885fe307984cb8b5802a652567c9d01d2d91649a66ea8ee93b9b88cfce804"
        || compiler_bytes != 949_248
    {
        return Err("The prepared compiler differs from the reviewed archive entry.".into());
    }
    Ok(serde_json::json!({
        "schemaVersion":1,
        "host":std::env::consts::OS,
        "architecture":std::env::consts::ARCH,
        "target":{
            "platform":prepared.target().platform_spec(),
            "board":prepared.target().board_id(),
            "framework":prepared.target().framework()
        },
        "preparedRoot":prepared.root().strip_prefix(&application).map_err(|error| error.to_string())?,
        "files":prepared.files().len(),
        "logicalBytes":total,
        "fileListSha256":format!("{:x}", aggregate.finalize()),
        "archiveFileListSha256":archive_hash,
        "fileListEncoding":"sorted UTF-8 path NUL decimal-size NUL sha256 NUL octal-mode LF",
        "materializedArchiveLinks":prepared.materialized_links(),
        "nativePackageMetadata":prepared.generated_metadata(),
        "packageLookupMetadataGenerated":packages,
        "compiler":{
            "sha256":compiler_hash,"bytes":compiler_bytes,
            "format":"ELF64 little-endian ET_EXEC x86_64"
        },
        "fixtureOnly":true,
        "archiveBytesPreserved":true,
        "regularFileRereadMatched":true,
        "privateRuntimeAdmitted":false,
        "installedPackageReadiness":false,
        "immutableInputLifetime":false,
        "toolOrCompilerExecution":false,
        "liveStoreOrHardwareAccess":false
    }))
}

fn hash_file(path: &Path) -> Result<(String, u64), String> {
    hash_reader(&mut File::open(path).map_err(|error| error.to_string())?)
}

fn hash_reader(reader: &mut impl Read) -> Result<(String, u64), String> {
    let mut hash = Sha256::new();
    let mut total = 0u64;
    let mut buffer = [0u8; 64 * 1024];
    loop {
        let count = reader
            .read(&mut buffer)
            .map_err(|error| error.to_string())?;
        if count == 0 {
            break;
        }
        total = total
            .checked_add(count as u64)
            .ok_or("Byte count overflowed")?;
        if total > 64 * 1024 * 1024 {
            return Err("A prepared file exceeded the probe bound.".into());
        }
        hash.update(&buffer[..count]);
    }
    Ok((format!("{:x}", hash.finalize()), total))
}
