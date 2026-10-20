//! Bounded ICO directory parsing for the embedded original application icon.

pub const APPLICATION_ICON: &[u8] = include_bytes!("../../../src/assets/mcu_icon.ico");

pub fn icon_image(bytes: &[u8], desired: u32) -> Result<&[u8], &'static str> {
    let invalid = "Invalid embedded ICO directory";
    if bytes.len() > 4 * 1024 * 1024 || bytes.get(..4) != Some(&[0, 0, 1, 0]) {
        return Err(invalid);
    }
    let count = u16::from_le_bytes(
        bytes
            .get(4..6)
            .ok_or(invalid)?
            .try_into()
            .map_err(|_| invalid)?,
    ) as usize;
    if !(1..=64).contains(&count) {
        return Err(invalid);
    }
    let end = 6 + count * 16;
    let directory = bytes.get(6..end).ok_or(invalid)?;
    let mut best = None;
    for entry in directory.as_chunks::<16>().0 {
        let width = if entry[0] == 0 {
            256
        } else {
            u32::from(entry[0])
        };
        let height = if entry[1] == 0 {
            256
        } else {
            u32::from(entry[1])
        };
        let length = u32::from_le_bytes(entry[8..12].try_into().map_err(|_| invalid)?) as usize;
        let offset = u32::from_le_bytes(entry[12..16].try_into().map_err(|_| invalid)?) as usize;
        if entry[3] != 0 || width != height || length == 0 || offset < end {
            return Err(invalid);
        }
        let image = bytes
            .get(offset..offset.checked_add(length).ok_or(invalid)?)
            .ok_or(invalid)?;
        // Prefer an exact/larger image before upscaling a smaller one.
        let score = (width < desired, width.abs_diff(desired));
        if best.as_ref().is_none_or(|(previous, _)| score < *previous) {
            best = Some((score, image));
        }
    }
    best.map(|(_, image)| image).ok_or(invalid)
}

#[cfg(test)]
mod checks {
    use super::*;

    #[test]
    fn original_icon_selects_small_large_and_high_dpi_images() {
        for (size, start, length) in [(16, 134, 612), (32, 2284, 1234), (256, 10250, 7342)] {
            assert_eq!(
                icon_image(APPLICATION_ICON, size).expect("original image"),
                &APPLICATION_ICON[start..start + length]
            );
        }
        assert_eq!(
            icon_image(APPLICATION_ICON, 30),
            icon_image(APPLICATION_ICON, 32)
        );
        assert_eq!(
            icon_image(APPLICATION_ICON, 1000),
            icon_image(APPLICATION_ICON, 256)
        );
    }

    #[test]
    fn malformed_directories_never_reach_native_icon_creation() {
        for length in [0, 5, 6, 21, APPLICATION_ICON.len() - 1] {
            assert!(icon_image(&APPLICATION_ICON[..length], 32).is_err());
        }
        for (index, value) in [(0, 1), (2, 2), (4, 0), (5, 255), (9, 1)] {
            let mut bytes = APPLICATION_ICON.to_vec();
            bytes[index] = value;
            assert!(icon_image(&bytes, 32).is_err());
        }
    }

    #[test]
    fn out_of_bounds_and_directory_overlapping_payloads_are_refused() {
        for (offset, replacement) in [(14, u32::MAX), (18, u32::MAX), (18, 6), (14, 0)] {
            let mut bytes = APPLICATION_ICON.to_vec();
            bytes[offset..offset + 4].copy_from_slice(&replacement.to_le_bytes());
            assert!(icon_image(&bytes, 32).is_err());
        }
    }
}
