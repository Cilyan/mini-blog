#!/usr/bin/env python
# coding: utf-8

import re
import json
import itertools

import pathlib
import mimetypes
mimetypes.init()

from PIL import Image
import imagehash

import pendulum
from pendulum.parsing.exceptions import ParserError

import jellyfish


_re_match_date_in_filename = re.compile((
    r"("
         r"((?P<date1>\d{4}-\d{2}-\d{2}) at (?P<time1>\d{2}\.\d{2}\.\d{2}))"
        r"|((?P<date2>\d{8})_?(?P<time2>\d{6}))"
        r"|((?P<date3>\d{8}))"
    r")"
))
_re_match_date_in_folder = re.compile(r"(?P<date>\d{4}_\d{2}_\d{2})")


class ImageInfos:
    
    def __init__(self, path, date, hash_, quality, size):
        self.path = path
        self.name = path.name
        self.date = date
        self.hash_ = hash_
        self.quality = quality
        self.size = size
    
    @classmethod
    def unserialize(cls, data):
        path = pathlib.Path(data["path"])
        date = None if data["date"] is None else pendulum.parse(data["date"])
        hash_ = imagehash.hex_to_hash(data["hash"])
        quality = data["quality"]
        size = (data["size"]["w"], data["size"]["h"])
        return cls(path, date, hash_, quality, size)
    
    @classmethod
    def make(cls, path):
        img = Image.open(path)
        hash_ = imagehash.phash(img)
        date = cls.guess_date(path, img)
        quality = img.info.get("quality")
        size = img.size
        return cls(path, date, hash_, quality, size)
    
    @staticmethod
    def guess_date(path, image):
        exif = image._getexif() if image else None
        exif_date = exif.get(306) if exif else None
        if exif_date:
            try:
                return pendulum.parse(exif_date, tz='Europe/Paris')
            except (ParserError, ValueError):
                pass
        matches = list(_re_match_date_in_filename.finditer(path.name))
        for m in reversed(matches):
            date = m["date1"] or m["date2"] or m["date3"]
            date = date.replace("-", "")
            time = m["time1"] or m["time2"] or ""
            time = time.replace(".", "")
            dt = date + " " + time if time else date
            try:
                return pendulum.parse(dt, tz='Europe/Paris')
            except (ParserError, ValueError):
                pass
        for p in reversed(path.parts):
            if (m := _re_match_date_in_folder.search(p)):
                try:
                    return pendulum.parse(m["date"], tz='Europe/Paris')
                except (ParserError, ValueError):
                    pass
        return None
    
    def serialize(self):
        return {
            "path": str(self.path),
            "name": self.name,
            "date": self.date.isoformat() if self.date else None,
            "hash": str(self.hash_),
            "quality": self.quality,
            "size": {"w": self.size[0], "h": self.size[1]},
        }


class ImageBucket:
    API = "1.0"
    def __init__(self):
        self.bucket = {}
    def add_path(self, path):
        imgi = ImageInfos.make(path)
        self.add(imgi)
    def add(self, imgi):
        h = imgi.hash_
        if h in self.bucket:
            imgis = self.bucket[h]
            imgis.append(imgi)
            print("Warning: hash({}) represents several images:".format(str(h)))
            for i in imgis:
                print("   ", i.path)
        else:
            self.bucket[h] = [imgi]
    def get(self, h):
        return self.bucket.get(h)
    def save(self, path):
        with open(path, "w", encoding="utf-8") as fout:
            json.dump(
                {
                    "version": self.API,
                    "images": [
                        img.serialize()
                        for img in itertools.chain.from_iterable(
                            self.bucket.values()
                        )
                    ]
                },
                fout,
                indent = 4
            )
    @classmethod
    def load(cls, path):
        self = cls()
        with open(path, "r", encoding="utf-8") as fin:
            data = json.load(fin)
            if not data["version"] == cls.API:
                raise ValueError("Bad version")
            for img in data["images"]:
                self.add(ImageInfos.unserialize(img))
        return self


class ImageMatcher:
    def __init__(self, bucket):
        self.bucket = bucket
        self.matched = []
    
    def match_add_path(self, path):
        imgi = ImageInfos.make(path)
        self.match_add(imgi)
    def match_add(self, imgi):
        matched = self.match(imgi)
        self.add(imgi, matched)
    def match_path(self, path):
        imgi = ImageInfos.make(path)
        return self.match(imgi)
    
    def match(self, imgi):
        candidates = self.bucket.get(imgi.hash_)
        if candidates is None:
            print("No candidates for", imgi.path)
            return None
        if len(candidates) == 1:
            return candidates[0]
        # reduce by best quality
        best_size = 0
        best_candidates = []
        for candidate in candidates:
            w, h = candidate.size
            size = w * h
            if size > best_size:
                best_size = size
                best_candidates = [candidate]
            elif size == best_size:
                best_candidates.append(candidate)
        if len(best_candidates) == 1:
            print(
                "Selected best quality candidate (out of",
                len(candidates),
                ") for",
                imgi.path
            )
            return best_candidates[0]
        # find best filename match
        best_distance = 1000000
        best_candidates_step2 = []
        for candidate in best_candidates:
            distance = jellyfish.damerau_levenshtein_distance(
                imgi.name,
                candidate.name
            )
            if distance < best_distance:
                best_distance = distance
                best_candidates_step2 = [candidate]
            elif distance == best_distance:
                best_candidates_step2.append(candidate)
        if len(best_candidates_step2) == 1:
            print(
                "Selected best candidate by name (out of",
                len(candidates),
                ") for",
                imgi.path
            )
            return best_candidates_step2[0]
        # Default
        print(
            "Selected best candidate by default (out of",
            len(candidates),
            ") for",
            imgi.path
        )
        return best_candidates_step2[0]
    
    def add(self, target, matched):
        self.matched.append((target, matched))
    
    def serialize(self):
        return [
            self._serialize1(target, matched)
            for target, matched in self.matched
        ]
    def _serialize1(self, target, matched):
        if matched:
            date = matched.date or target.date
            mpath = str(matched.path)
            w, h = matched.size
            msize = {"w": w, "h": h}
        else:
            date = target.date
            mpath = None
            msize = None
        w, h = target.size
        return {
            "date" : date.isoformat() if date else None,
            "hash" : str(target.hash_),
            "path_target" : str(target.path),
            "path_matched" : mpath,
            "size_target" : {"w": w, "h": h},
            "size_matched" : msize
        }

# ## Create Metadata for Original Images
def create_metadata(path, savefile):
    root = pathlib.Path(path)

    bucket = ImageBucket()

    any_images = (
        f for f in root.rglob("*")
        if (
            (mt := mimetypes.guess_type(f)[0]) is not None
            and mt.startswith("image")
        )
    )
    images = (
        img for img in any_images
        if ("Blob" not in img.parts) and ("resize" not in img.parts)
    )
    for f in images:
        bucket.add_path(f)

    bucket.save(savefile)


# ## Match Photos
def load_bucket(savefile):
    bucket = ImageBucket.load(savefile)
    return bucket

def match_images(path, savefile, bucket):
    rootfda = pathlib.Path(path)

    matcher = ImageMatcher(bucket)

    fda_images = (
        f for f in rootfda.rglob("*")
        if (mt := mimetypes.guess_type(f)[0]) is not None and mt.startswith("image")
    )
    for f in fda_images:
        matcher.match_add_path(f)

    with open(savefile, "w", encoding="utf-8") as fout:
        data = {
            "version": "1.0",
            "root": str(rootfda),
            "matches": matcher.serialize()
        }
        json.dump(data, fout, indent = 4)


def main():
    import sys
    import toml
    import os.path

    if len(sys.argv) != 2:
        print("Usage: {} path/to/site".format(sys.argv[0]))
        sys.exit(-1)

    configpath = os.path.join(
        os.path.abspath(sys.argv[1]),
        "config_site.toml"
    )
    with open(configpath, 'r', encoding="utf-8") as fin:
        config = toml.load(fin)
        photoscfg = config.get("photos", {})
    
    cache = photoscfg.get("cache", "")
    originals = photoscfg.get("originals", "")
    force_recreate_cache = photoscfg.get("force_recreate_cache", True)
    if (not os.path.exists(cache)) or force_recreate_cache:
        create_metadata(originals, cache)
    
    bucket = load_bucket(cache)

    metadata = os.path.join(
        os.path.abspath(sys.argv[1]),
        photoscfg.get("metadata", "matcherdata.json")
    )
    downloaded = os.path.join(
        os.path.abspath(sys.argv[1]),
        "static",
        photoscfg.get("path", "photos")
    )
    match_images(downloaded, metadata, bucket)
    sys.exit(0)

if __name__ == "__main__":
    main()