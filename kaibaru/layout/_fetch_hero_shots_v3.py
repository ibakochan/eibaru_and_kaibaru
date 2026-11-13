#!/usr/bin/env python3
"""Download five 1-2 person stills for each remaining template hero."""
from pathlib import Path
import urllib.request

BASE = "https://storage.googleapis.com/sitepicsforkaibaru/"
OUT = Path("/mnt/c/Users/user/AppData/Local/Temp/kaibaru-hero-v3")

# Templates that already have the realistic multi-clip heroes are omitted:
# striking, yoga, bjj, music.
SHOTS = {
    "default": [
        "layouts/default/about.jpg",
        "layouts/default/slide-members.jpg",
        "layouts/default/slide-group.jpg",
        "layouts/default/slide-entrance.jpg",
        "layouts/default/about.jpg",
    ],
    "karate": [
        "layouts/karate/adults.jpg",
        "layouts/karate/kids-kata.jpg",
        "layouts/karate/seniors.jpg",
        "layouts/karate/slide-practice.jpg",
        "layouts/karate/slide-bow.jpg",
    ],
    "fitness": [
        "layouts/fitness/boxercise.jpg",
        "layouts/fitness/hiit.jpg",
        "layouts/fitness/stretch.jpg",
        "layouts/fitness/slide-smiles.jpg",
        "layouts/fitness/about.jpg",
    ],
    "gym": [
        "layouts/gym/personal-training.jpg",
        "layouts/gym/member-training.jpg",
        "layouts/gym/slide-orientation.jpg",
        "layouts/gym/free-weights.jpg",
        "layouts/gym/machines.jpg",
    ],
    "cram": [
        "layouts/cram/slide-teaching.jpg",
        "layouts/cram/elementary.jpg",
        "layouts/cram/junior-high.jpg",
        "layouts/cram/high-school.jpg",
        "layouts/cram/slide-study.jpg",
    ],
    "eikaiwa": [
        "layouts/eikaiwa/one-on-one.jpg",
        "layouts/eikaiwa/smiling-lesson.jpg",
        "layouts/eikaiwa/kids.jpg",
        "layouts/eikaiwa/adults.jpg",
        "layouts/eikaiwa/business.jpg",
    ],
    "dance": [
        "layouts/dance/hiphop.jpg",
        "layouts/dance/jazz.jpg",
        "layouts/dance/kids.jpg",
        "layouts/dance/slide-lesson.jpg",
        "layouts/dance/contest.jpg",
    ],
    "swimming": [
        "layouts/swimming/slide-coach.jpg",
        "layouts/swimming/adult-lesson.jpg",
        "layouts/swimming/juniors.jpg",
        "layouts/swimming/toddler-class.jpg",
        "layouts/swimming/slide-adults.jpg",
    ],
    "sports": [
        "layouts/sports/youth-match.jpg",
        "layouts/sports/adults.jpg",
        "layouts/sports/juniors.jpg",
        "layouts/sports/slide-practice.jpg",
        "layouts/sports/slide-team.jpg",
    ],
    "hair_salon": [
        "layouts/hair_salon/haircut.jpg",
        "layouts/hair_salon/color.jpg",
        "layouts/hair_salon/perm.jpg",
        "layouts/hair_salon/consultation.jpg",
        "layouts/hair_salon/stylist.jpg",
    ],
    "nail_salon": [
        "layouts/nail_salon/manicure.jpg",
        "layouts/nail_salon/gel.jpg",
        "layouts/nail_salon/care.jpg",
        "layouts/nail_salon/art.jpg",
        "layouts/nail_salon/artist.jpg",
    ],
}


def main():
    opener = urllib.request.build_opener()
    opener.addheaders = [("User-Agent", "Mozilla/5.0")]
    urllib.request.install_opener(opener)
    for folder, names in SHOTS.items():
        dest_dir = OUT / folder
        dest_dir.mkdir(parents=True, exist_ok=True)
        for index, name in enumerate(names, start=1):
            dest = dest_dir / f"shot{index}.jpg"
            print(f"get {folder}/shot{index}.jpg <- {name}")
            urllib.request.urlretrieve(BASE + name, dest)
            print(f"  {dest.stat().st_size}")
    print("done", len(SHOTS), "templates")


if __name__ == "__main__":
    main()
