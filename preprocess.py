#!/usr/bin/env python3
"""
DICOM Preprocessor: Converts DICOM files to PNGs and extracts metadata.
Outputs organized PNG files and a metadata.json for the viewer.
"""

import os
import sys
import json
import hashlib
import datetime
from pathlib import Path
from collections import defaultdict

import numpy as np
import pydicom
from PIL import Image

BASE_DIR = Path(__file__).parent.parent
DICOM_DIR = BASE_DIR / "DICOM"
REPORTS_DIR = BASE_DIR / "REPORTS"
OUTPUT_DIR = Path(__file__).parent / "processed"


def apply_windowing(pixel_array, window_center, window_width):
    """Apply DICOM windowing to pixel data."""
    if window_width <= 0:
        raise ValueError(f"window_width must be > 0, got {window_width}")
    img_min = window_center - window_width / 2
    img_max = window_center + window_width / 2
    if img_max - img_min <= 0:
        raise ValueError(f"Invalid window parameters: center={window_center}, width={window_width}")
    windowed = np.clip(pixel_array, img_min, img_max)
    windowed = ((windowed - img_min) / (img_max - img_min) * 255).astype(np.uint8)
    return windowed


def get_default_window(pixel_array, modality):
    """Compute reasonable default window if DICOM headers don't specify."""
    if modality == "CT":
        return 40, 80  # Brain window
    p5, p95 = np.percentile(pixel_array[pixel_array > 0], [5, 95]) if np.any(pixel_array > 0) else (0, 255)
    center = (p5 + p95) / 2
    width = max(p95 - p5, 1)
    return center, width


def safe_str(val):
    """Convert DICOM value to clean string."""
    if val is None:
        return ""
    s = str(val).strip()
    if s in ("None", ""):
        return ""
    return s


def safe_int(val, default=0):
    """Safely cast a DICOM value to int, returning default on failure."""
    try:
        return int(val)
    except (TypeError, ValueError):
        return default


def safe_float(val, default=0.0):
    """Safely cast a DICOM value to float, returning default on failure."""
    try:
        return float(val)
    except (TypeError, ValueError):
        return default


def process_dicom_file(filepath):
    """Read a DICOM file and return metadata + pixel data."""
    try:
        ds = pydicom.dcmread(str(filepath), force=True)
    except Exception as e:
        return None, f"Read error: {e}"

    if not hasattr(ds, "pixel_array"):
        return None, "No pixel data"

    try:
        pixels = ds.pixel_array.astype(np.float64)
    except Exception as e:
        return None, f"Pixel decode error: {e}"

    # Check for all-zeros (damaged files from disc recovery)
    if np.all(pixels == 0):
        return None, "All-zero pixel data (damaged)"

    # Apply rescale slope/intercept if present
    slope = float(getattr(ds, "RescaleSlope", 1))
    intercept = float(getattr(ds, "RescaleIntercept", 0))
    pixels = pixels * slope + intercept

    # Get windowing parameters
    wc = getattr(ds, "WindowCenter", None)
    ww = getattr(ds, "WindowWidth", None)
    modality = safe_str(getattr(ds, "Modality", ""))

    if wc is not None and ww is not None:
        wc = float(wc[0]) if isinstance(wc, pydicom.multival.MultiValue) else float(wc)
        ww = float(ww[0]) if isinstance(ww, pydicom.multival.MultiValue) else float(ww)
    else:
        wc, ww = get_default_window(pixels, modality)

    img_data = apply_windowing(pixels, wc, ww)

    # Handle multi-frame
    if len(img_data.shape) == 3 and img_data.shape[0] > 1:
        # Multi-frame - take middle frame
        img_data = img_data[img_data.shape[0] // 2]

    metadata = {
        "patientName": safe_str(getattr(ds, "PatientName", "")),
        "patientID": safe_str(getattr(ds, "PatientID", "")),
        "patientBirthDate": safe_str(getattr(ds, "PatientBirthDate", "")),
        "patientSex": safe_str(getattr(ds, "PatientSex", "")),
        "studyDate": safe_str(getattr(ds, "StudyDate", "")),
        "studyTime": safe_str(getattr(ds, "StudyTime", "")),
        "studyDescription": safe_str(getattr(ds, "StudyDescription", "")),
        "studyInstanceUID": safe_str(getattr(ds, "StudyInstanceUID", "")),
        "seriesDescription": safe_str(getattr(ds, "SeriesDescription", "")),
        "seriesInstanceUID": safe_str(getattr(ds, "SeriesInstanceUID", "")),
        "seriesNumber": safe_int(getattr(ds, "SeriesNumber", 0)),
        "instanceNumber": safe_int(getattr(ds, "InstanceNumber", 0)),
        "modality": modality,
        "institution": safe_str(getattr(ds, "InstitutionName", "")),
        "referringPhysician": safe_str(getattr(ds, "ReferringPhysicianName", "")),
        "accession": safe_str(getattr(ds, "AccessionNumber", "")),
        "bodyPart": safe_str(getattr(ds, "BodyPartExamined", "")),
        "rows": safe_int(getattr(ds, "Rows", 0)),
        "columns": safe_int(getattr(ds, "Columns", 0)),
        "windowCenter": wc,
        "windowWidth": ww,
        "sliceLocation": safe_float(getattr(ds, "SliceLocation", 0.0) if hasattr(ds, "SliceLocation") else 0.0),
    }

    return (metadata, img_data), None


def load_reports():
    """Load radiology report text files."""
    reports = {}
    if not REPORTS_DIR.exists():
        return reports
    for txt_file in REPORTS_DIR.rglob("*.TXT"):
        content = txt_file.read_text(encoding="utf-8", errors="replace").strip()
        # Classify by the EXAM: line which is the definitive study type
        exam_line = ""
        for line in content.split("\n"):
            if line.strip().upper().startswith("EXAM:"):
                exam_line = line.strip().upper()
                break
        if exam_line.startswith("EXAM: CT") or exam_line.startswith("EXAM:CT"):
            reports["CT"] = content
        elif exam_line.startswith("EXAM: MR") or exam_line.startswith("EXAM:MR"):
            reports["MR"] = content
        elif "CHEST" in exam_line or "X RAY" in exam_line or "XR " in exam_line:
            reports["CR"] = content
        else:
            key = txt_file.stem
            reports[key] = content
    return reports


def main():
    print("DICOM Preprocessor")
    print("=" * 60)

    # Collect all DICOM files
    dicom_files = []
    for root, dirs, files in os.walk(DICOM_DIR):
        for fname in files:
            fpath = Path(root) / fname
            if fpath.stat().st_size > 1000:  # Skip tiny files
                dicom_files.append(fpath)

    print(f"Found {len(dicom_files)} DICOM files")

    # Process all files
    studies = defaultdict(lambda: defaultdict(list))
    patient_info = {}
    skipped = 0
    errors = 0

    for i, fpath in enumerate(dicom_files):
        if (i + 1) % 100 == 0:
            print(f"  Processing {i + 1}/{len(dicom_files)}...")

        result, err = process_dicom_file(fpath)
        if err:
            if "damaged" in err.lower():
                skipped += 1
            else:
                errors += 1
            continue

        meta, img_data = result
        study_uid = meta["studyInstanceUID"]
        series_uid = meta["seriesInstanceUID"]
        studies[study_uid][series_uid].append((meta, img_data))

        if not patient_info:
            patient_info = {
                "name": meta["patientName"],
                "id": meta["patientID"],
                "birthDate": meta["patientBirthDate"],
                "sex": meta["patientSex"],
            }

    print(f"\nProcessed: {len(dicom_files) - skipped - errors} OK, {skipped} damaged, {errors} errors")
    print(f"Studies: {len(studies)}, Total series: {sum(len(s) for s in studies.values())}")

    # Organize and save PNGs
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    all_studies = []
    for study_uid, series_dict in studies.items():
        # Get study-level info from first image
        first_meta = next(iter(next(iter(series_dict.values()))))[0]
        modality = first_meta["modality"]
        study_desc = first_meta["studyDescription"]
        uid_suffix = hashlib.md5(study_uid.encode()).hexdigest()[:8]
        study_dir_name = f"{modality}_{study_desc}_{uid_suffix}".replace(" ", "_").replace("/", "-")[:58]

        study_info = {
            "uid": study_uid,
            "date": first_meta["studyDate"],
            "time": first_meta["studyTime"],
            "description": study_desc,
            "modality": modality,
            "institution": first_meta["institution"],
            "referringPhysician": first_meta["referringPhysician"],
            "accession": first_meta["accession"],
            "series": [],
        }

        series_list = sorted(series_dict.items(), key=lambda x: next(iter(x[1]))[0]["seriesNumber"])

        for series_idx, (series_uid, images) in enumerate(series_list):
            # Sort by instance number, then slice location
            images.sort(key=lambda x: (x[0]["instanceNumber"], x[0]["sliceLocation"]))

            series_meta = images[0][0]
            series_dir = OUTPUT_DIR / study_dir_name / f"series_{series_idx + 1:03d}"
            series_dir.mkdir(parents=True, exist_ok=True)

            image_paths = []
            for img_idx, (meta, img_data) in enumerate(images):
                png_name = f"slice_{img_idx + 1:04d}.png"
                png_path = series_dir / png_name
                Image.fromarray(img_data).save(str(png_path), optimize=True)
                image_paths.append(f"{study_dir_name}/series_{series_idx + 1:03d}/{png_name}")

            series_info = {
                "uid": series_uid,
                "number": series_meta["seriesNumber"],
                "description": series_meta["seriesDescription"],
                "bodyPart": series_meta["bodyPart"],
                "sliceCount": len(images),
                "rows": series_meta["rows"],
                "columns": series_meta["columns"],
                "images": image_paths,
            }
            study_info["series"].append(series_info)

            print(f"  {modality} / {series_meta['seriesDescription']}: {len(images)} slices")

        all_studies.append(study_info)

    # Sort studies by date/time
    all_studies.sort(key=lambda x: (x["date"], x["time"]))

    # Load reports
    reports = load_reports()

    # Build final metadata
    metadata = {
        "patient": patient_info,
        "studies": all_studies,
        "reports": reports,
        "recoveryInfo": {
            "totalFiles": len(dicom_files),
            "intactFiles": len(dicom_files) - skipped - errors,
            "damagedFiles": skipped,
            "errorFiles": errors,
            "recoveryDate": datetime.date.today().isoformat(),
            "sourceMedia": "DVD-R from UT Southwestern Medical Center",
        },
    }

    meta_path = OUTPUT_DIR / "metadata.json"
    with open(meta_path, "w") as f:
        json.dump(metadata, f, indent=2)

    print(f"\nMetadata saved to: {meta_path}")
    print(f"PNGs saved to: {OUTPUT_DIR}")
    print("Done!")


if __name__ == "__main__":
    main()
