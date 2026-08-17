# Copyright (c) 2026 Paul H. Breslin
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Lesser General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the
# GNU Lesser General Public License for more details.
#
# You should have received a copy of the GNU Lesser General Public License
# along with this program. If not, see <https://www.gnu.org/licenses/>.
#
# --------------------------------------------------------------------------
# Web-service refactor notes:
#   * Added connect(desc_url): a non-interactive equivalent of the old
#     select_server()'s "I already know the URL" path. select_server()
#     (still used by the CLI) now delegates to it so there's one code
#     path for "point the browser at this server description URL".
#   * Diagnostic print()s now go through `logging`. The interactive menu
#     text in select_server()/start_ui() (actual CLI UI, not diagnostics)
#     stays as print()/input() since that's presentation, not logging.
# --------------------------------------------------------------------------
import socket
import requests
import xml.etree.ElementTree as ET
from urllib.parse import urljoin, urlparse
import re
import os
import logging

logger = logging.getLogger("dlnabrowser")


def classify_media_type(upnp_class, protocol_info):
    """Broad category for a file item -- 'audio', 'video', 'image', or
    'other' -- used only to pick a display icon in the browse list, never
    for compatibility/filtering decisions (real-world renderers are
    notoriously inconsistent about accurately reporting what they
    actually support, so nothing here gates what can be queued).

    Prefers upnp:class: a required, reliably-populated DIDL-Lite field,
    unlike the res element's protocolInfo/DLNA profile reporting which
    varies a lot between server and renderer implementations. Falls back
    to the MIME type parsed out of protocolInfo (protocol:network:
    contentFormat:additionalInfo) if upnp:class is missing or an
    unrecognized value."""
    if upnp_class:
        if "audioItem" in upnp_class:
            return "audio"
        if "videoItem" in upnp_class:
            return "video"
        if "imageItem" in upnp_class:
            return "image"

    if protocol_info:
        parts = protocol_info.split(":")
        if len(parts) >= 3:
            mime = parts[2]
            if mime.startswith("audio/"):
                return "audio"
            if mime.startswith("video/"):
                return "video"
            if mime.startswith("image/"):
                return "image"

    return "other"


class DLNABrowser:
    """Handles parsing the media tree from UPnP/DLNA media servers."""
    DEFAULT_DESC_URL = "http://192.168.132.5:8200/rootDesc.xml"
    NS_SOAP = "http://schemas.xmlsoap.org/soap/envelope/"
    NS_CD = "urn:schemas-upnp-org:service:ContentDirectory:1"
    NS_DIDL_UPNP = "urn:schemas-upnp-org:metadata-1-0/upnp/"

    def __init__(self):
        self.desc_url = None
        self.control_url = None
        self.friendly_name = "None"
        self.history = []
        self.current_id = "0"
        self.current_title = "Root"
        self.cache = {}

    @staticmethod
    def get_friendly_name(desc_url):
        """Fetches the device's UPnP description XML to retrieve its friendly
        name (e.g. "MiniDLNA on nas", "Jellyfin Server") -- same standard
        <friendlyName> element DLNARenderer reads, just for a media server's
        own description document rather than a renderer's."""
        try:
            r = requests.get(desc_url, timeout=2)
            r.raise_for_status()
            ns = {'upnp': 'urn:schemas-upnp-org:device-1-0'}
            root = ET.fromstring(r.content)

            friendly_name_node = root.find('.//upnp:friendlyName', ns)
            if friendly_name_node is not None:
                name = friendly_name_node.text
                if name and "$(hostname)" in name:
                    actual_host = urlparse(desc_url).hostname or "Unknown"
                    name = name.replace("$(hostname)", actual_host)
                return name
        except Exception as e:
            logger.debug(f"Could not fetch friendly name from {desc_url}: {e}")
        return "Unknown Media Server"

    @staticmethod
    def discover_servers(timeout=3):
        """SSDP scan targeting ContentDirectory services."""
        logger.info("Broadcasting SSDP M-SEARCH for Content Directory services...")
        search_target = DLNABrowser.NS_CD

        ssdp_request = (
            "M-SEARCH * HTTP/1.1\r\n"
            "HOST: 239.255.255.250:1900\r\n"
            "MAN: \"ssdp:discover\"\r\n"
            f"ST: {search_target}\r\n"
            f"MX: {timeout}\r\n"
            "\r\n"
        )

        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
        sock.settimeout(timeout)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)

        try:
            sock.sendto(ssdp_request.encode('utf-8'), ("239.255.255.250", 1900))
        except Exception as e:
            logger.warning(f"Failed to send SSDP request: {e}")
            return []

        discovered = {}
        try:
            while True:
                data, addr = sock.recvfrom(2048)
                response = data.decode('utf-8', errors='ignore')

                location_match = re.search(r'(?i)LOCATION:\s*(http://\S+)', response)
                st_match = re.search(r'(?i)ST:\s*(\S+)', response)
                usn_match = re.search(r'(?i)USN:\s*(\S+)', response)

                if location_match and st_match and st_match.group(1) == search_target:
                    loc_url = location_match.group(1)
                    usn = usn_match.group(1) if usn_match else loc_url
                    discovered[usn] = loc_url
        except socket.timeout:
            pass
        except Exception as e:
            logger.warning(f"Error during discovery capture: {e}")
        finally:
            sock.close()

        logger.info(f"Media server discovery found {len(discovered)} device(s).")
        return list(discovered.values())

    def connect(self, desc_url):
        """Non-interactive connection to a media server, given its description
        XML URL. Resets browse state (history/cache/position) since we may be
        pointing at a different server than before. Returns True/False and
        never raises -- an unreachable media server is an expected condition
        (server not running, wrong URL), not a fatal one."""
        self.desc_url = desc_url
        self.friendly_name = self.get_friendly_name(desc_url)
        self.history = []
        self.current_id = "0"
        self.current_title = "Root"
        self.cache = {}
        return self._fetch_control_url()

    def select_server(self):
        """Discovers servers and lets user pick an active ContentDirectory server. (CLI only.)"""
        urls = self.discover_servers()
        if not urls:
            print("[-] No UPnP Media Servers discovered via SSDP.")
            fallback = input(f"Enter server XML description URL [{self.DEFAULT_DESC_URL}]: ").strip()
            chosen = fallback if fallback else self.DEFAULT_DESC_URL
        elif len(urls) == 1:
            chosen = urls[0]
            print(f"[+] Automatically selected only available server: {chosen}")
        else:
            print("\n Discovered Media Servers:")
            for idx, url in enumerate(urls, 1):
                print(f"   {idx}. {url}")
            try:
                choice = int(input(f"Select a server (1-{len(urls)}): "))
                chosen = urls[choice - 1]
            except (ValueError, IndexError):
                chosen = urls[0]
                print(f"[!] Invalid selection. Defaulting to first server: {chosen}")

        return self.connect(chosen)

    def _fetch_control_url(self):
        """Queries description XML to grab the matching ContentDirectory controlURL."""
        try:
            r = requests.get(self.desc_url, timeout=3)
            r.raise_for_status()

            control_path = None
            try:
                root = ET.fromstring(r.content)
                for service in root.findall(".//service"):
                    st = service.find("serviceType")
                    if st is not None and self.NS_CD in st.text:
                        cu = service.find("controlURL")
                        if cu is not None:
                            control_path = cu.text
                            break
            except Exception:
                pass

            if not control_path:
                match = re.search(r'<serviceType>' + re.escape(self.NS_CD) + r'.*?<controlURL>(.*?)</controlURL>',
                                  r.text, re.DOTALL)
                if match:
                    control_path = match.group(1).strip()

            if control_path:
                self.control_url = urljoin(self.desc_url, control_path)
                return True

            logger.warning("Found server, but it doesn't expose a ContentDirectory control interface.")
            return False
        except Exception as e:
            logger.warning(f"Failed parsing server description ({self.desc_url}): {e}")
            return False

    def browse_container(self, container_id):
        """Sends a SOAP Browse action request to the Media Server container."""
        if container_id in self.cache:
            return self.cache[container_id]

        soap_payload = (
            f'<?xml version="1.0" encoding="utf-8"?>\n'
            f'<s:Envelope xmlns:s="{self.NS_SOAP}" s:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/">\n'
            f'  <s:Body>\n'
            f'    <u:Browse xmlns:u="{self.NS_CD}">\n'
            f'      <ObjectID>{container_id}</ObjectID>\n'
            f'      <BrowseFlag>BrowseDirectChildren</BrowseFlag>\n'
            f'      <Filter>*</Filter>\n'
            f'      <StartingIndex>0</StartingIndex>\n'
            f'      <RequestedCount>0</RequestedCount>\n'
            f'      <SortCriteria></SortCriteria>\n'
            f'    </u:Browse>\n'
            f'  </s:Body>\n'
            f'</s:Envelope>'
        )

        headers = {
            "Content-Type": 'text/xml; charset="utf-8"',
            "SOAPACTION": f'"{self.NS_CD}#Browse"',
            "Connection": "close"
        }

        try:
            r = requests.post(self.control_url, data=soap_payload, headers=headers, timeout=4)
            r.raise_for_status()

            result_xml = None
            try:
                root = ET.fromstring(r.content)
                result_node = root.find(".//Result")
                if result_node is not None:
                    result_xml = result_node.text
            except Exception:
                pass

            if not result_xml:
                match = re.search(r'<Result>(.*?)</Result>', r.text, re.DOTALL)
                if match:
                    import html
                    result_xml = html.unescape(match.group(1))

            if not result_xml:
                return []

            items = []
            try:
                didl_root = ET.fromstring(result_xml)
                for el in didl_root:
                    tag = el.tag.split('}')[-1]
                    if tag == 'container':
                        items.append(('folder', {
                            'id': el.attrib.get('id'),
                            'title': el.find('{http://purl.org/dc/elements/1.1/}title').text
                        }))
                    elif tag == 'item':
                        res_node = el.find('{urn:schemas-upnp-org:metadata-1-0/DIDL-Lite/}res')
                        uri = res_node.text if res_node is not None else ""
                        protocol_info = res_node.attrib.get('protocolInfo') if res_node is not None else None
                        class_node = el.find(f'{{{self.NS_DIDL_UPNP}}}class')
                        upnp_class = class_node.text if class_node is not None else None
                        items.append(('file', {
                            'id': el.attrib.get('id'),
                            'title': el.find('{http://purl.org/dc/elements/1.1/}title').text,
                            'uri': uri,
                            'media_type': classify_media_type(upnp_class, protocol_info),
                        }))
            except Exception:
                folders = re.findall(r'<container\s+id="([^"]+)"[^>]*>.*?<dc:title>([^<]+)</dc:title>', result_xml, re.DOTALL)
                for fid, ftitle in folders:
                    items.append(('folder', {'id': fid, 'title': ftitle}))

                files = re.findall(
                    r'<item\s+id="([^"]+)"[^>]*>.*?<dc:title>([^<]+)</dc:title>'
                    r'(?:.*?<upnp:class>([^<]*)</upnp:class>)?'
                    r'.*?<res([^>]*)>([^<]+)</res>',
                    result_xml, re.DOTALL
                )
                for fid, ftitle, uclass, res_attrs, furi in files:
                    pi_match = re.search(r'protocolInfo="([^"]*)"', res_attrs)
                    protocol_info = pi_match.group(1) if pi_match else None
                    items.append(('file', {
                        'id': fid, 'title': ftitle, 'uri': furi,
                        'media_type': classify_media_type(uclass or None, protocol_info),
                    }))

            self.cache[container_id] = items
            return items

        except Exception as e:
            logger.warning(f"Browse error on container '{container_id}' (media server may be offline): {e}")
            return []

    def count_tracks(self, container_id):
        """Direct-child track count for a folder -- deliberately NOT
        recursive (queueing an entire subtree at once is explicitly out
        of scope, since it's how a single click could queue thousands of
        tracks). Backs both the "Queue All" cap and the per-folder
        "Queue N Tracks" buttons.

        Goes through browse_container(), so once warm_cache() has run at
        startup this is an in-memory count with no network round-trip --
        the whole point of pre-scanning the library."""
        items = self.browse_container(container_id)
        return sum(1 for item_type, _ in items if item_type == "file")

    def warm_cache(self, progress_callback=None, progress_every=10):
        """Recursively walks the entire folder tree from the root, browsing
        every container so its contents land in self.cache -- the exact
        same cache browse_container() already checks first, so this is
        purely "do the lazy-loading work upfront" rather than a separate
        cache mechanism. After this runs, browsing anywhere in the tree
        is instant (no SOAP round-trip) for the rest of the process's life.

        Deliberately sequential, not parallel: many DLNA servers (MiniDLNA
        especially) are a single modest daemon that doesn't handle
        concurrent Browse requests gracefully, so a firehose of parallel
        requests risks queuing delays or timeouts that would eat back any
        speed gained from parallelism -- one at a time is the safer default.

        progress_callback(folders_scanned, tracks_found), if given, is
        called periodically (every `progress_every` folders, plus always
        once at the end) so a caller can report live progress without
        it firing so often it floods whatever it's reporting to (e.g. an
        SSE stream).

        Returns (folders_scanned, tracks_found).
        """
        folders_scanned = 0
        tracks_found = 0
        to_visit = [self.current_id]
        visited = set()

        while to_visit:
            container_id = to_visit.pop(0)
            if container_id in visited:
                continue
            visited.add(container_id)

            items = self.browse_container(container_id)
            folders_scanned += 1

            for item_type, data in items:
                if item_type == "folder":
                    to_visit.append(data["id"])
                else:
                    tracks_found += 1

            if progress_callback and (folders_scanned % progress_every == 0):
                progress_callback(folders_scanned, tracks_found)

        if progress_callback:
            progress_callback(folders_scanned, tracks_found)

        logger.info(f"Library crawl complete: {folders_scanned} folders, {tracks_found} tracks cached.")
        return folders_scanned, tracks_found

    def _get_relative_path(self, item_title, item_uri):
        """Reconstructs the real relative file path from browser history breadcrumbs."""
        ignore_nodes = ("root", "music", "folders", "audio")
        path_segments = [title for (_, title) in self.history if title.lower() not in ignore_nodes]

        if self.current_title.lower() not in ignore_nodes:
            path_segments.append(self.current_title)

        parsed_url = urlparse(item_uri)
        filename = os.path.basename(parsed_url.path) if parsed_url.path else f"{item_title}.mp3"

        if filename.endswith(".mp3") or filename.endswith(".flac") or filename.endswith(".ogg") or filename.endswith(".m4a"):
            path_segments.append(filename)
        else:
            path_segments.append(f"{item_title}.mp3")

        return "/".join(path_segments)

    def display_folder(self, menu_items):
        """Prints directory/file menu items cleanly to the terminal screen. (CLI only.)"""
        print(f"\n📁 BROWSER: {self.current_title} (ID: {self.current_id})")
        print("─" * 60)

        if self.history:
            print("   0.  ⬅️  .. [Go Back]")

        for idx, (item_type, item_data) in enumerate(menu_items, 1):
            icon = "📁" if item_type == 'folder' else "🎵"
            print(f"  {idx:2d}. {icon} {item_data['title']}")
        print("─" * 60)

    def enqueue_item_by_index(self, menu_items, index, play_queue):
        """Validates index values and pushes targeted file nodes to the PlayQueue. (CLI only.)"""
        if index < 0 or index >= len(menu_items):
            print(f" [!] Selection {index + 1} is out of bounds.")
            return False

        item_type, item_data = menu_items[index]
        if item_type == 'file':
            item_data['relative_path'] = self._get_relative_path(item_data['title'], item_data['uri'])
            play_queue.add_to_queue(item_data)
            return True

        print(f" [!] Selection {index + 1} is a folder; skipped.")
        return False

    def handle_batch_enqueue(self, menu_items, raw_tokens, play_queue):
        """Parses batch multi-selections and range expressions. (CLI only.)"""
        target_indices = []

        if not raw_tokens:
            raw_tokens = [str(i) for i in range(1, len(menu_items) + 1)]

        for token in raw_tokens:
            if '-' in token:
                try:
                    start, end = map(int, token.split('-'))
                    target_indices.extend(range(start - 1, end))
                except ValueError:
                    pass
            elif token.isdigit():
                target_indices.append(int(token) - 1)

        queued_count = sum(1 for idx in target_indices if self.enqueue_item_by_index(menu_items, idx, play_queue))
        print(f"\n[+] Batch complete. Enqueued {queued_count} files.")

    def handle_navigation(self, menu_items, user_choice):
        """Evaluates folder selection or checks history back-tracking conditions. (CLI only.)"""
        try:
            idx = int(user_choice)
            if self.history and idx == 0:
                self.current_id, self.current_title = self.history.pop()
                return

            array_idx = idx - 1
            if array_idx < 0 or array_idx >= len(menu_items):
                print("\n[!] Invalid selection index range.")
                return

            item_type, item_data = menu_items[array_idx]
            if item_type == 'folder':
                self.history.append((self.current_id, self.current_title))
                self.current_id = item_data['id']
                self.current_title = item_data['title']
            elif item_type == 'file':
                print("\n[!] To queue single tracks, select them directly or use batch processing.")

        except ValueError:
            print("\n[!] Invalid entry format.")

    def start_ui(self, play_queue):
        """Active interactive browse selection interface. (CLI only.)"""
        while True:
            menu_items = self.browse_container(self.current_id)
            self.display_folder(menu_items)

            print(" Navigation Options:")
            print("   <num>    : Navigate into a folder")
            print("   q        : Queue ALL tracks found in the folder")
            print("   q <nums> : Queue tracks selectively (e.g., 'q 1', 'q 3 5 7-12')")
            print("   b        : Exit browser view back to playback controls")

            choice = input("\nChoose an option: ").strip()
            if not choice:
                continue
            if choice.lower() == 'b':
                break

            if choice.lower() == 'q' or choice.lower().startswith('q '):
                tokens = choice[2:].split() if choice.lower().startswith('q ') else []
                self.handle_batch_enqueue(menu_items, tokens, play_queue)
            else:
                self.handle_navigation(menu_items, choice)
