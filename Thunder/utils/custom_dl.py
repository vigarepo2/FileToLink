# Thunder/utils/custom_dl.py

import math
import asyncio
from typing import Dict, Union
from pyrogram import Client, utils, raw
from pyrogram.session import Session, Auth
from pyrogram.errors import AuthBytesInvalid, RPCError, FloodWait
from pyrogram.file_id import FileId, FileType, ThumbnailSource
from Thunder.vars import Var
from Thunder.bot import work_loads
from Thunder.server.exceptions import FileNotFound
from .file_properties import get_file_ids
from Thunder.utils.logger import logger

class ByteStreamer:
    """
    A custom class that handles streaming of media files from Telegram servers.

    Attributes:
        client (Client): The Pyrogram client instance.
        clean_timer (int): Interval in seconds to clean the cache.
        cached_file_ids (Dict[int, FileId]): A cache for file properties.
        cache_lock (asyncio.Lock): An asyncio lock to ensure thread-safe access to the cache.
    """

    def __init__(self, client: Client):
        """
        Initialize the ByteStreamer with a Pyrogram client.

        Args:
            client (Client): The Pyrogram client instance.
        """
        self.client = client
        self.clean_timer = 30 * 60  # Cache clean interval in seconds (30 minutes)
        self.cached_file_ids: Dict[int, FileId] = {}
        self.cache_lock = asyncio.Lock()
        asyncio.create_task(self.clean_cache())
        logger.info("ByteStreamer initialized with client.")

    async def get_file_properties(self, message_id: int) -> FileId:
        """
        Get file properties from cache or generate if not available.

        Args:
            message_id (int): The message ID of the file.

        Returns:
            FileId: The file properties object.

        Raises:
            FileNotFound: If the file is not found in the channel.
        """
        logger.debug(f"Fetching file properties for message ID {message_id}.")
        async with self.cache_lock:
            file_id = self.cached_file_ids.get(message_id)
        
        if not file_id:
            logger.debug(f"File ID for message {message_id} not found in cache, generating...")
            file_id = await self.generate_file_properties(message_id)
            async with self.cache_lock:
                self.cached_file_ids[message_id] = file_id
            logger.info(f"Cached new file properties for message ID {message_id}.")
        
        return file_id

    async def generate_file_properties(self, message_id: int) -> FileId:
        """
        Generate file properties for a given message ID.

        Args:
            message_id (int): The message ID of the file.

        Returns:
            FileId: The file properties object.

        Raises:
            FileNotFound: If the file is not found.
        """
        logger.debug(f"Generating file properties for message ID {message_id}.")
        file_id = await get_file_ids(self.client, Var.BIN_CHANNEL, message_id)
        
        if not file_id:
            logger.warning(f"Message ID {message_id} not found in the channel.")
            raise FileNotFound(f"File with message ID {message_id} not found.")
        
        async with self.cache_lock:
            self.cached_file_ids[message_id] = file_id
        logger.info(f"Generated and cached file properties for message ID {message_id}.")
        
        return file_id

    async def generate_media_session(self, file_id: FileId) -> Session:
        """
        Generate the media session for the DC that contains the media file.

        Args:
            file_id (FileId): The file properties object.

        Returns:
            Session: The media session object.

        Raises:
            AuthBytesInvalid: If authentication fails after retries.
        """
        logger.debug(f"Generating media session for DC {file_id.dc_id}.")
        client = self.client
        media_session = client.media_sessions.get(file_id.dc_id)

        if media_session is None:
            client_dc_id = await client.storage.dc_id()
            test_mode = await client.storage.test_mode()

            if file_id.dc_id != client_dc_id:
                logger.info(f"Creating new media session for DC {file_id.dc_id}.")
                auth = Auth(client, file_id.dc_id, test_mode)
                auth_key = await auth.create()
                media_session = Session(
                    client,
                    file_id.dc_id,
                    auth_key,
                    test_mode,
                    is_media=True,
                )
                await media_session.start()

                for attempt in range(6):
                    try:
                        exported_auth = await client.invoke(
                            raw.functions.auth.ExportAuthorization(dc_id=file_id.dc_id)
                        )
                        await media_session.send(
                            raw.functions.auth.ImportAuthorization(
                                id=exported_auth.id, bytes=exported_auth.bytes
                            )
                        )
                        logger.info(f"Authorization imported for DC {file_id.dc_id} on attempt {attempt + 1}.")
                        break
                    except AuthBytesInvalid:
                        logger.warning(f"AuthBytesInvalid on attempt {attempt + 1} for DC {file_id.dc_id}.")
                        if attempt == 5:
                            await media_session.stop()
                            logger.error(f"Failed after 6 attempts for DC {file_id.dc_id}.")
                            raise AuthBytesInvalid(f"Failed after 6 attempts for DC {file_id.dc_id}")
                        await asyncio.sleep(2 ** attempt)  # Exponential backoff
                    except FloodWait as e:
                        logger.warning(f"FloodWait encountered: sleeping for {e.value} seconds.")
                        await asyncio.sleep(e.value + 1)
                    except RPCError as e:
                        logger.error(f"RPCError during auth for DC {file_id.dc_id}: {e}")
                        await asyncio.sleep(1)
            else:
                logger.info(f"Using existing auth key for DC {file_id.dc_id}.")
                media_session = Session(
                    client,
                    file_id.dc_id,
                    await client.storage.auth_key(),
                    test_mode,
                    is_media=True,
                )
                await media_session.start()

            client.media_sessions[file_id.dc_id] = media_session
        else:
            logger.debug(f"Using cached media session for DC {file_id.dc_id}.")

        return media_session

    @staticmethod
    async def get_location(file_id: FileId) -> Union[
        raw.types.InputPhotoFileLocation,
        raw.types.InputDocumentFileLocation,
        raw.types.InputPeerPhotoFileLocation,
    ]:
        """
        Get the appropriate location object for the file type.

        Args:
            file_id (FileId): The file properties object.

        Returns:
            Union[InputPhotoFileLocation, InputDocumentFileLocation, InputPeerPhotoFileLocation]: The location object.
        """
        logger.debug(f"Determining location for file type {file_id.file_type}.")
        file_type = file_id.file_type

        if file_type == FileType.CHAT_PHOTO:
            if file_id.chat_id > 0:
                peer = raw.types.InputPeerUser(
                    user_id=file_id.chat_id, access_hash=file_id.chat_access_hash
                )
            else:
                if file_id.chat_access_hash == 0:
                    peer = raw.types.InputPeerChat(chat_id=-file_id.chat_id)
                else:
                    peer = raw.types.InputPeerChannel(
                        channel_id=utils.get_channel_id(file_id.chat_id),
                        access_hash=file_id.chat_access_hash,
                    )
            location = raw.types.InputPeerPhotoFileLocation(
                peer=peer,
                volume_id=file_id.volume_id,
                local_id=file_id.local_id,
                big=file_id.thumbnail_source == ThumbnailSource.CHAT_PHOTO_BIG,
            )
        elif file_type == FileType.PHOTO:
            location = raw.types.InputPhotoFileLocation(
                id=file_id.media_id,
                access_hash=file_id.access_hash,
                file_reference=file_id.file_reference,
                thumb_size=file_id.thumbnail_size,
            )
        else:
            location = raw.types.InputDocumentFileLocation(
                id=file_id.media_id,
                access_hash=file_id.access_hash,
                file_reference=file_id.file_reference,
                thumb_size=file_id.thumbnail_size,
            )
        logger.debug(f"Location determined for file ID {file_id.media_id}.")
        return location

    async def yield_file(
        self,
        file_id: FileId,
        index: int,
        offset: int,
        first_part_cut: int,
        last_part_cut: int,
        part_count: int,
        chunk_size: int,
    ) -> Union[bytes, None]:
        """
        Yield chunks of a file, handling cuts at the first and last parts.

        Args:
            file_id (FileId): The file properties object.
            index (int): The client index.
            offset (int): The offset to start reading from.
            first_part_cut (int): The number of bytes to cut from the first chunk.
            last_part_cut (int): The number of bytes to cut from the last chunk.
            part_count (int): The total number of parts.
            chunk_size (int): The size of each chunk.

        Yields:
            bytes: The next chunk of data.
        """
        client = self.client
        work_loads[index] += 1
        logger.debug(f"Starting to yield file with client index {index}.")

        media_session = await self.generate_media_session(file_id)
        current_part = 1
        location = await self.get_location(file_id)

        try:
            while current_part <= part_count:
                try:
                    response = await media_session.send(
                        raw.functions.upload.GetFile(
                            location=location, offset=offset, limit=chunk_size
                        )
                    )
                    if not isinstance(response, raw.types.upload.File):
                        logger.warning("Unexpected response type while fetching file.")
                        break

                    chunk = response.bytes
                    if not chunk:
                        logger.debug("Received empty chunk, ending stream.")
                        break

                    if part_count == 1:
                        yield chunk[first_part_cut:last_part_cut]
                    elif current_part == 1:
                        yield chunk[first_part_cut:]
                    elif current_part == part_count:
                        yield chunk[:last_part_cut]
                    else:
                        yield chunk

                    current_part += 1
                    offset += chunk_size

                    if current_part > part_count:
                        break

                except FloodWait as e:
                    logger.warning(f"FloodWait encountered: sleeping for {e.value} seconds.")
                    await asyncio.sleep(e.value + 1)
                except (RPCError, asyncio.TimeoutError) as e:
                    logger.error(f"Error while fetching file part: {e}")
                    raise
        finally:
            logger.debug(f"Finished yielding file, processed {current_part - 1} parts.")
            work_loads[index] -= 1

    async def clean_cache(self) -> None:
        """
        Periodically clean the cache of stored file IDs.
        """
        while True:
            await asyncio.sleep(self.clean_timer)
            async with self.cache_lock:
                self.cached_file_ids.clear()
            logger.debug("Cache cleaned.")
