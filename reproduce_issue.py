import io
import sys
import zipfile
from pathlib import Path

from adeu.models import ReviewAction
from adeu.redline.engine import RedlineEngine


def inspect_for_duplicates(docx_path: str):
    print(f"\n--- Inspecting {docx_path} for duplicate parts ---")
    duplicates_found = False
    with zipfile.ZipFile(docx_path, "r") as z:
        file_list = z.namelist()

        # Check for the specific duplicates known to cause issues
        dup_patterns = [
            ("word/comments.xml", "word/comments1.xml"),
            ("word/commentsIds.xml", "word/commentsIds1.xml"),
            ("word/commentsExtended.xml", "word/commentsExtended1.xml"),
            ("word/commentsExtensible.xml", "word/commentsExtensible1.xml"),
        ]

        for original, duplicate in dup_patterns:
            has_orig = original in file_list
            has_dup = duplicate in file_list

            if has_orig and has_dup:
                print(f"❌ FAIL: Found duplicate parts: '{original}' AND '{duplicate}'")
                duplicates_found = True
            elif has_dup and not has_orig:
                print(
                    f"⚠️  WARN: Found '{duplicate}' but not original. This might be a fresh file, or original was lost."
                )
            elif has_orig:
                print(f"✅ OK: Found '{original}' without duplicate.")

    if duplicates_found:
        print("\n[CONCLUSION] The bug is REPRODUCED. Word will likely ignore the new part.")
    else:
        print("\n[CONCLUSION] No duplicates found. File might be valid (or both parts missing).")


def main():
    base_dir = Path("tests/fixtures")
    input_path = base_dir / "golden.docx"
    output_path = Path("testing.docx")

    if not input_path.exists():
        print(f"Error: Input file not found: {input_path}")
        sys.exit(1)

    print(f"Loading {input_path}...")
    with open(input_path, "rb") as f:
        stream = io.BytesIO(f.read())

    engine = RedlineEngine(stream, author="Mikko Korpela")

    # Reply to the last known comment in golden.docx (Com:2)
    # Goal: Create Com:3
    print("Applying Reply to 'Com:2'...")
    action = ReviewAction(action="REPLY", target_id="Com:2", text="Forth comment")

    applied, skipped = engine.apply_review_actions([action])
    print(f"Applied: {applied}, Skipped: {skipped}")

    if applied == 0:
        print("Error: Action was skipped. Check if target ID exists.")

    print(f"Saving to {output_path}...")
    with open(output_path, "wb") as f:
        f.write(engine.save_to_stream().getvalue())

    # Verify
    inspect_for_duplicates(str(output_path))


if __name__ == "__main__":
    main()
