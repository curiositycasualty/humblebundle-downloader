"""What the walk works with: a bundle, its products, their downloads
and the files in them, as typed values rather than nested dicts.

humble_site still hands bundles over in the old api's shape (see
legacy_order there), because that is what the cache keys and much of
the history are built on. They are turned into these once, where the
walk begins, so nothing past that point reaches into dicts by key.
"""
from dataclasses import dataclass, field

from .files import _coerce_size, _struct_filename, _struct_hashes

# What a file entry is
FILE = "file"                # an ordinary file at a url
ASMJS = "asmjs"              # a game playable in the browser
EXTERNAL = "external"        # a link to somewhere else
UNKNOWN = "unknown"          # nothing hbd knows how to fetch


@dataclass(frozen=True)
class Signing:
    """How to get a download url for a file from the rebuilt site,
    which hands out filenames and signs urls only when asked"""

    entitlement: str
    download_key: str
    vault: bool = False


@dataclass(frozen=True)
class FileEntry:
    kind: str
    filename: object = None              # str for FILE
    url: object = None                   # what the listing gives, for FILE
    listed_size: object = None           # bytes, as Humble lists it
    hashes: dict = field(default_factory=dict)
    signing: object = None               # Signing, on the rebuilt site
    struct: dict = field(default_factory=dict, compare=False, repr=False)

    @classmethod
    def from_struct(cls, struct):
        """One entry of a download's file list, in the old api's shape"""
        url = (struct.get("url") or {}).get("web") if isinstance(struct.get("url"), dict) else None
        if url:
            signing = struct.get("hbd_sign")
            return cls(
                kind=FILE,
                filename=_struct_filename(struct),
                url=url,
                listed_size=_coerce_size(struct.get("file_size")),
                hashes=_struct_hashes(struct),
                signing=Signing(
                    signing["entitlement"],
                    signing["download_key"],
                    bool(signing.get("vault")),
                ) if signing else None,
                struct=struct,
            )
        if "asm_config" in struct:
            return cls(kind=ASMJS, struct=struct)
        if "external_link" in struct:
            return cls(kind=EXTERNAL, url=struct["external_link"], struct=struct)
        return cls(kind=UNKNOWN, struct=struct)


@dataclass(frozen=True)
class Download:
    """Everything a product offers for one platform"""

    platform: str
    files: tuple

    @classmethod
    def from_legacy(cls, download):
        return cls(
            platform=download.get("platform") or "",
            files=tuple(FileEntry.from_struct(s) for s in download.get("download_struct") or ()),
        )


@dataclass(frozen=True)
class Product:
    title: str
    downloads: tuple

    @classmethod
    def from_legacy(cls, product):
        return cls(
            title=product["human_name"],
            downloads=tuple(Download.from_legacy(d) for d in product.get("downloads") or ()),
        )


@dataclass(frozen=True)
class Order:
    """A bundle, or the vault"""

    id: str
    title: str
    products: tuple

    @classmethod
    def from_legacy(cls, order_id, order):
        return cls(
            id=order_id,
            title=order["product"]["human_name"],
            products=tuple(Product.from_legacy(p) for p in order.get("subproducts") or ()),
        )
