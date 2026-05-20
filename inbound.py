import streamlit as st
import pandas as pd
import requests
from datetime import datetime
from zoneinfo import ZoneInfo
from io import StringIO, BytesIO
import base64
from PIL import Image
from auth import check_password

# --- WEBHOOK URLs ---
WEBHOOK_URL_PHOTO = "https://script.google.com/macros/s/AKfycbx_iDTXWOfy0k1xBgxWF7MMqV1h4_wyGiXidDQj-qI8S_GQXTKfiFtwMK3tk10hfWR6/exec"
WEBHOOK_URL_DATA = "https://script.google.com/macros/s/AKfycbx_iDTXWOfy0k1xBgxWF7MMqV1h4_wyGiXidDQj-qI8S_GQXTKfiFtwMK3tk10hfWR6/exec"

# --- GOOGLE SHEETS ---
VESSEL_SHEET_ID = "18rlYmNpArAvEZrD3yyy7iAFDpHvFqEvN7pvztb1VcVM"
VESSEL_SHEET_NAME = "Vessel Name"
VESSEL_CSV_URL = f"https://docs.google.com/spreadsheets/d/{VESSEL_SHEET_ID}/gviz/tq?tqx=out:csv&sheet={VESSEL_SHEET_NAME}"

# --- Hardcoded Lists ---
pic_list = [
    "Ananta Aditya",
    "Muhamad Nur Budi",
    "Deni Kristianto", 
    "Abdul Hadi",
    "Ali Yusni",
    "Irawansyah",
    "Ronal",
    "Jordan",
    "Miranda Puji",
    "Azis Prasetya"
]
db_list = ["DMI", "PBN", "PKS", "PMT", "PSS", "PSM", "PST"]
condition_list = ["Good", "Damaged", "Incomplete", "Needs Review"]

# --- Upload / compression policy ---
MAX_BYTES_PER_FILE = 8 * 1024 * 1024   # 8 MB raw file allowed (before compression check)
COMPRESS_MAX_WIDTH = 1600              # max image width in pixels (resize if wider)
COMPRESS_QUALITY = 80                  # JPEG quality (0-100)
UPLOAD_TIMEOUT = 60                    # seconds for requests.post
UPLOAD_RETRIES = 3                     # number of attempts per upload


# === Utility functions ===

@st.cache_data(ttl=600) # Cache the CSV load so it doesn't slow down the app on every interaction
def load_csv(url: str) -> pd.DataFrame:
    """Load CSV data from a URL into a Pandas DataFrame."""
    try:
        resp = requests.get(url, timeout=15)
        resp.raise_for_status()
        return pd.read_csv(StringIO(resp.text))
    except Exception as e:
        st.error(f"Failed to load Vessel data: {e}")
        return pd.DataFrame()


def compress_image_bytes(file_bytes: bytes, max_width=COMPRESS_MAX_WIDTH, quality=COMPRESS_QUALITY) -> bytes:
    """Compress / resize image bytes and return new JPEG bytes."""
    try:
        img = Image.open(BytesIO(file_bytes)).convert("RGB")
    except Exception:
        return file_bytes

    if img.width > max_width:
        ratio = max_width / float(img.width)
        new_size = (int(img.width * ratio), int(img.height * ratio))
        img = img.resize(new_size, Image.LANCZOS)

    out_buf = BytesIO()
    try:
        img.save(out_buf, format="JPEG", quality=quality, optimize=True)
    except Exception:
        out_buf = BytesIO()
        img.save(out_buf, format="PNG", optimize=True)
    return out_buf.getvalue()


def post_with_retries(url: str, json_payload: dict, timeout=UPLOAD_TIMEOUT, retries=UPLOAD_RETRIES):
    """POST with simple exponential backoff retries."""
    import time
    backoff = 1
    last_exc = None
    for attempt in range(1, retries + 1):
        try:
            resp = requests.post(url, json=json_payload, timeout=timeout)
            return resp
        except requests.RequestException as e:
            last_exc = e
            if attempt == retries:
                raise
            time.sleep(backoff)
            backoff *= 2
    if last_exc:
        raise last_exc


def upload_photos_to_drive(uploaded_files, folder_name, progress_text):
    """
    Handles processing, compressing, and sequentially uploading a list of files.
    Returns: (is_success: bool, folder_url: str, error_messages: list)
    """
    if not uploaded_files:
        return True, "", []

    processed_images = []
    errors = []

    # 1. Process & Compress
    for file in uploaded_files:
        try:
            raw_bytes = file.read()
        except Exception as e:
            errors.append(f"Failed to read file {file.name}: {e}")
            return False, "UPLOAD_FAILED", errors

        if len(raw_bytes) > MAX_BYTES_PER_FILE:
            compressed = compress_image_bytes(raw_bytes)
            if len(compressed) > MAX_BYTES_PER_FILE:
                errors.append(f"File {file.name} is too large after compression. Max per-file: {MAX_BYTES_PER_FILE/1024/1024:.0f} MB.")
                return False, "UPLOAD_FAILED", errors
        else:
            compressed = compress_image_bytes(raw_bytes)

        processed_images.append({"filename": file.name, "bytes": compressed})

    # 2. Sequential Upload
    drive_folder_url = "UPLOAD_FAILED"
    my_bar = st.progress(0, text=f"{progress_text} (0/{len(processed_images)})")

    for idx, img in enumerate(processed_images):
        b64 = base64.b64encode(img["bytes"]).decode("utf-8")
        payload = {
            "folder_name": folder_name,
            "images": [{"filename": img["filename"], "content": b64}]
        }

        try:
            resp = post_with_retries(WEBHOOK_URL_PHOTO, payload)
        except Exception as e:
            errors.append(f"Network/error uploading {img['filename']}: {e}")
            my_bar.empty()
            return False, "UPLOAD_FAILED", errors

        if resp.status_code != 200:
            errors.append(f"Server returned {resp.status_code} for {img['filename']}: {resp.text[:500]}")
            my_bar.empty()
            return False, "UPLOAD_FAILED", errors

        # Robust Link Extraction
        try:
            j = resp.json()
            if isinstance(j, dict) and drive_folder_url == "UPLOAD_FAILED":
                drive_folder_url = j.get("folderUrl", drive_folder_url)
        except Exception:
            pass

        progress_percentage = int(((idx + 1) / len(processed_images)) * 100)
        my_bar.progress(progress_percentage, text=f"{progress_text} ({idx + 1}/{len(processed_images)})")

    my_bar.empty()
    return True, drive_folder_url, []


# === Streamlit UI ===
if check_password():
    st.set_page_config(page_title="Incoming Data Log", layout="wide") # Must be the first Streamlit command!
    st.title("📥 Incoming Data Log")

    # --- Basic Info ---
    col1, col2 = st.columns(2)
    with col1:
        selected_pic = st.selectbox("PIC :", [""] + pic_list)
    with col2:
        selected_db = st.selectbox("Database:", [""] + db_list)

    st.markdown("---")

    # --- Item, Qty & Vessel Details ---
    st.subheader("Item Details")
    
    # Load and filter Vessel Data based on selected Database
    df_vessel = load_csv(VESSEL_CSV_URL)
    vessels_for_db = (
        df_vessel[df_vessel["DB"].astype(str).str.strip() == selected_db]
        if not df_vessel.empty and selected_db
        else pd.DataFrame()
    )
    vessel_options = (
        sorted(vessels_for_db["Vessel Name"].dropna().astype(str).unique().tolist())
        if "Vessel Name" in vessels_for_db.columns
        else []
    )

    col3, col4 = st.columns(2)
    with col3:
        jumlah_item = st.number_input("Number of Items", min_value=0, step=1, value=0)
        nomor_po = st.text_input("Nomor PO")
        nama_vendor = st.text_input("Nama Vendor")
    with col4:
        jumlah_qty = st.number_input("Quantity", min_value=0, step=1, value=0)
        selected_condition = st.selectbox("Condition:", condition_list)
        
        # Display Vessel selectbox or text input properly
        if not vessel_options:
            vessel_name = st.text_input("Vessel Name (no entry in sheet, type manually):")
        else:
            vessel_name = st.selectbox("Vessel Name:", vessel_options)

    st.markdown("---")

    # --- Photo Uploads ---
    st.subheader("Documentation")
    
    st.markdown("**1. DO / Surat Jalan / Pick List Photos**")
    uploaded_do_files = st.file_uploader("Upload DO/Surat Jalan photos:", accept_multiple_files=True, type=["jpg", "jpeg", "png"], key="do_uploader")
    
    st.markdown("**2. Item Photos**")
    uploaded_item_files = st.file_uploader("Upload Photos of the Items:", accept_multiple_files=True, type=["jpg", "jpeg", "png"], key="item_uploader")
    
    st.caption(f"Per-file limit: {MAX_BYTES_PER_FILE//1024//1024} MB. Unlimited total files allowed.")

    # --- Submit Button ---
    if st.button("✅ Submit", type="primary"):
        # Basic validation
        if not selected_pic:
            st.warning("Please select a PIC.")
            st.stop()
        if not selected_db:
            st.warning("Please select a Database.")
            st.stop()
        if not nomor_po.strip():
            st.warning("Please input Nomor PO.")
            st.stop()
        if not uploaded_do_files:
            st.warning("Please upload at least one DO/Surat Jalan photo.")
            st.stop()
        if not uploaded_item_files:
            st.warning("Please upload at least one photo of the items.")
            st.stop()

        # Prepare folder names and timestamp
        timestamp = datetime.now(ZoneInfo("Asia/Jakarta")).strftime("%d/%m/%Y")
        safe_po = "".join([c for c in nomor_po if c.isalnum() or c in ("-", "_")])
        
        do_folder_name = f"Inbound_DO_{selected_db}_{safe_po}"
        item_folder_name = f"Inbound_Items_{selected_db}_{safe_po}"

        # === Step 1: Upload DO Photos ===
        st.info("Initiating upload for DO/Surat Jalan photos...")
        do_success, do_url, do_errors = upload_photos_to_drive(uploaded_do_files, do_folder_name, "Uploading DO Photos")
        
        if not do_success:
            st.error("DO Photo upload failed. Submission aborted.")
            for e in do_errors:
                st.write(f"- {e}")
            st.stop()

        # === Step 2: Upload Item Photos ===
        st.info("Initiating upload for Item photos...")
        item_success, item_url, item_errors = upload_photos_to_drive(uploaded_item_files, item_folder_name, "Uploading Item Photos")
        
        if not item_success:
            st.error("Item Photo upload failed. Submission aborted.")
            for e in item_errors:
                st.write(f"- {e}")
            st.stop()

        # === Step 3: Send Data Payload ===
        st.info("Sending data payload...")
        data_payload = {
            "timestamp": timestamp,
            "PIC": selected_pic,
            "database": selected_db,
            "jumlah_item": jumlah_item,
            "jumlah_qty": float(jumlah_qty),
            "condition": selected_condition,
            "nama_vendor": nama_vendor,
            "vessel_id": vessel_name,
            "nomor_po": nomor_po,
            "do_folder_link": do_url,
            "item_folder_link": item_url
        }

        try:
            data_response = requests.post(WEBHOOK_URL_DATA, json=data_payload, timeout=30)
            if data_response.status_code == 200:
                st.success("🎉 Submission completed successfully!")
                st.markdown(f"**DO Folder Link:** [📂 View Files]({do_url})")
                st.markdown(f"**Items Folder Link:** [📂 View Files]({item_url})")
            else:
                st.error(f"❌ Data logging failed: {data_response.status_code} - {data_response.text[:500]}")
        except Exception as e:
            st.error(f"❌ Logging error: {e}")
