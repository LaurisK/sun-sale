"""Generic typed persistent store wrapping HA's Store helper."""
from __future__ import annotations

from datetime import datetime
from typing import Any, Callable, Generic, TypeVar

from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store

T = TypeVar("T")
JSONDict = dict[str, Any]


class PersistentStore(Generic[T]):
    """Typed wrapper around HA's Store with optional list append-and-trim semantics.

    Folds the raw HA Store, in-memory cache, and serialisation logic into one
    object so callers need only declare a single field per persisted value.

    Args:
        hass: Home Assistant instance.
        version: Storage version integer passed to HA's Store.
        key: Storage key string passed to HA's Store.
        serialize: Convert T to a JSON-serialisable dict.
        deserialize: Reconstruct T from the stored dict; may return None to
            signal that the stored data should be treated as missing.
    """

    def __init__(
        self,
        hass: HomeAssistant,
        version: int,
        key: str,
        serialize: Callable[[T], JSONDict],
        deserialize: Callable[[JSONDict], T | None],
    ) -> None:
        """Initialise store with serialisation callbacks; does not perform I/O."""
        self._store: Store = Store(hass, version, key)
        self._serialize = serialize
        self._deserialize = deserialize
        self._value: T | None = None

    @property
    def value(self) -> T | None:
        """Return cached in-memory value (None until first load or save)."""
        return self._value

    async def load(self) -> T | None:
        """Load from disk, deserialise, cache, and return.

        Returns:
            Deserialised value, or None if the store is empty.
        """
        raw = await self._store.async_load()
        if raw is None:
            return None
        self._value = self._deserialize(raw)
        return self._value

    async def save(self, value: T) -> None:
        """Serialise value, write to disk, and update the in-memory cache.

        Args:
            value: Value to persist.
        """
        self._value = value
        await self._store.async_save(self._serialize(value))

    async def append_and_trim(
        self,
        new_item: Any,
        cutoff: datetime,
        timestamp_fn: Callable[[Any], datetime],
    ) -> None:
        """Append an item, discard entries older than cutoff, then save.

        Only valid when T is list[Item].

        Args:
            new_item: Item to append to the list.
            cutoff: Entries with a timestamp strictly before this are removed.
            timestamp_fn: Extracts the datetime key from each list element.
        """
        items: list = list(self._value or [])
        items = [x for x in items if timestamp_fn(x) >= cutoff]
        items.append(new_item)
        await self.save(items)  # type: ignore[arg-type]
