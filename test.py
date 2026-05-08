# FILE: prof_batch.py
import cProfile
import pstats
import time
from io import BytesIO
from pathlib import Path

from adeu.models import ModifyText
from adeu.redline.engine import RedlineEngine


def main():
    target_file = r"C:\Users\Uzair\Desktop\VVBIG.docx"

    if not Path(target_file).exists():
        print(f"Error: {target_file} not found.")
        return

    print(f"Loading {target_file} into memory...")
    with open(target_file, "rb") as f:
        stream_bytes = f.read()

    print("Initializing RedlineEngine...")
    t0 = time.time()

    # Engine initialization includes parsing the XML and the first mapper build
    engine = RedlineEngine(BytesIO(stream_bytes), author="QA Agent")

    t1 = time.time()
    print(f"Engine Init took {t1 - t0:.2f} seconds.")

    # The exact change that timed out
    changes = [
        ModifyText(
            type="modify",
            target_text="Use Ctrl+F to search on words or phrases.",
            new_text="Use Ctrl+F to **rapidly locate** specific words or phrases within this Handbook.",
            comment="QA Phase 2: Verifying that modify+bold+comment operations work.",
        )
    ]

    print("Processing batch... (Profiling)")
    profiler = cProfile.Profile()
    profiler.enable()

    t2 = time.time()
    stats = engine.process_batch(changes)

    profiler.disable()
    t3 = time.time()

    print(f"Process Batch took {t3 - t2:.2f} seconds.")
    print(f"Result stats: {stats}")

    print("\n--- TOP 25 TIME-CONSUMING FUNCTIONS ---")
    ps = pstats.Stats(profiler).sort_stats("cumtime")
    ps.print_stats(25)


if __name__ == "__main__":
    main()
