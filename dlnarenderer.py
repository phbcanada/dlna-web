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
# Web-service refactor notes (see original CLI version for comparison):
#   * print() calls used for diagnostics/errors now go through `logging`,
#     so both the CLI and the web app's log panel get them.
#   * _send_command / pause / stop / resume now return True/False instead
#     of returning None unconditionally, so callers (PlayQueue, the Flask
#     routes) can tell the difference between "it worked" and "the
#     renderer didn't respond" -- essential once a renderer can be
#     offline/asleep, which is the normal state for a device that isn't
#     always on.
#   * No behavioral change to the interactive select_renderer() CLI flow.
# --------------------------------------------------------------------------
import socket
import requests
import xml.etree.ElementTree as ET
from urllib.parse import urljoin, urlparse
import re
import html
import logging

logger = logging.getLogger("dlnarenderer")


class DLNARenderer:
    """Handles UPnP AVTransport devices (Renderers/Players) for controlling playback."""
    NS_SOAP = "http://schemas.xmlsoap.org/soap/envelope/"
    NS_AVT = "urn:schemas-upnp-org:service:AVTransport:1"
    NS_CM = "urn:schemas-upnp-org:service:ConnectionManager:1"
    NS_RC = "urn:schemas-upnp-org:service:RenderingControl:1"

    def __init__(self):
        self.desc_url = None
        self.control_url = None
        self.friendly_name = "None"
        self.connection_manager_control_url = None
        # None until resolve_control_url() finds a RenderingControl
        # service entry -- and even then, having a control URL only means
        # the service is *advertised*. Some renderers implement it but
        # reject the actual volume actions (see get_volume()'s docstring),
        # so this alone isn't the volume-support signal; get_volume()
        # returning a real value is.
        self.rendering_control_url = None
        # Captured for potential future use -- NOT currently used for any
        # compatibility filtering/gating decision. Real-world renderers
        # are inconsistent enough about accurately reporting this
        # (it's literally its own DLNA certification test point) that we
        # deliberately don't act on it yet; see get_sink_protocol_info().
        self.sink_protocol_info = []

    @property
    def host(self):
        """Extracts and returns the hostname or IP address of the renderer."""
        if self.desc_url:
            return urlparse(self.desc_url).hostname
        return "Unknown"

    @staticmethod
    def discover_renderers(timeout=3):
        """SSDP scan targeting AVTransport services."""
        logger.info("Broadcasting SSDP M-SEARCH for AVTransport Renderers...")
        search_target = DLNARenderer.NS_AVT

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

        discovered_urls = set()
        try:
            sock.sendto(ssdp_request.encode('utf-8'), ("239.255.255.250", 1900))
            while True:
                try:
                    data, addr = sock.recvfrom(1024)
                    response = data.decode('utf-8', errors='ignore')
                    loc_match = re.search(r'(?i)LOCATION:\s*(http\S+)', response)
                    if loc_match:
                        discovered_urls.add(loc_match.group(1))
                except socket.timeout:
                    break
        except Exception as e:
            logger.warning(f"SSDP renderer socket error: {e}")
        finally:
            sock.close()

        logger.info(f"Renderer discovery found {len(discovered_urls)} device(s).")
        return list(discovered_urls)

    @staticmethod
    def get_friendly_name(desc_url):
        """Fetches the XML description of the renderer to retrieve its friendly name."""
        try:
            r = requests.get(desc_url, timeout=2)
            r.raise_for_status()
            ns = {'upnp': 'urn:schemas-upnp-org:device-1-0'}
            root = ET.fromstring(r.content)

            friendly_name_node = root.find('.//upnp:friendlyName', ns)
            if friendly_name_node is not None:
                name = friendly_name_node.text

                # Check for and substitute unresolved $(hostname) strings
                if name and "$(hostname)" in name:
                    actual_host = urlparse(desc_url).hostname or "Unknown"
                    name = name.replace("$(hostname)", actual_host)
                return name
        except Exception as e:
            logger.debug(f"Could not fetch friendly name from {desc_url}: {e}")
        return "Unknown DLNA Renderer"

    def resolve_control_url(self, desc_url):
        """Parses description XML to locate the AVTransport control and event URLs.
        Raises on failure (unreachable device, missing AVTransport service) --
        callers are expected to catch this, since a not-currently-on renderer
        is an expected, recoverable condition, not a fatal one."""
        r = requests.get(desc_url, timeout=5)
        r.raise_for_status()

        ns = {'upnp': 'urn:schemas-upnp-org:device-1-0'}
        root = ET.fromstring(r.content)

        parsed_desc = urlparse(desc_url)
        base_url = f"http://{parsed_desc.netloc}"

        # Initialize default placeholders using the local base_url string
        self.avtransport_event_url = f"{base_url}/upnp/event/rendertransport1"
        self.rendering_control_event_url = f"{base_url}/upnp/event/rendercontrol1"

        avtransport_found = False

        for service in root.findall('.//upnp:service', ns):
            s_type_node = service.find('upnp:serviceType', ns)
            if s_type_node is None:
                continue

            service_type = s_type_node.text

            event_path_node = service.find('upnp:eventSubURL', ns)
            if event_path_node is not None and event_path_node.text:
                event_url = event_path_node.text
                if not event_url.startswith("http"):
                    event_url = f"{base_url}/{event_url.lstrip('/')}"

                if "AVTransport" in service_type:
                    self.avtransport_event_url = event_url
                    logger.info(f"Found AVTransport Event URL: {event_url}")
                elif "RenderingControl" in service_type:
                    self.rendering_control_event_url = event_url
                    logger.info(f"Found RenderingControl Event URL: {event_url}")

            if "AVTransport:1" in service_type:
                control_path = service.find('upnp:controlURL', ns).text
                self.desc_url = desc_url
                self.control_url = urljoin(desc_url, control_path)
                self.friendly_name = self.get_friendly_name(desc_url)
                avtransport_found = True

            if "ConnectionManager:1" in service_type:
                cm_control_node = service.find('upnp:controlURL', ns)
                if cm_control_node is not None and cm_control_node.text:
                    self.connection_manager_control_url = urljoin(desc_url, cm_control_node.text)

            if "RenderingControl:1" in service_type:
                rc_control_node = service.find('upnp:controlURL', ns)
                if rc_control_node is not None and rc_control_node.text:
                    self.rendering_control_url = urljoin(desc_url, rc_control_node.text)

        if avtransport_found:
            if self.connection_manager_control_url:
                self.get_sink_protocol_info()  # best-effort; never blocks selection
            return self.control_url

        raise Exception("AVTransport service not found on this device.")

    def get_sink_protocol_info(self):
        """Fetches the renderer's supported playback formats via
        ConnectionManager::GetProtocolInfo -- captured on self.sink_protocol_info
        for potential future use, but nothing currently reads it. Safe to
        fail silently: many renderers implement this action inconsistently
        or not at all, which is fine precisely because nothing depends on
        it succeeding."""
        soap = f"""<?xml version="1.0" encoding="utf-8"?>
<s:Envelope xmlns:s="{self.NS_SOAP}" s:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/">
  <s:Body>
    <u:GetProtocolInfo xmlns:u="{self.NS_CM}"></u:GetProtocolInfo>
  </s:Body>
</s:Envelope>"""
        headers = {
            "Content-Type": 'text/xml; charset="utf-8"',
            "SOAPACTION": f'"{self.NS_CM}#GetProtocolInfo"',
            "Connection": "close"
        }
        try:
            r = requests.post(self.connection_manager_control_url, data=soap, headers=headers, timeout=4)
            r.raise_for_status()
            root = ET.fromstring(r.content)
            sink_node = root.find(".//Sink")
            if sink_node is not None and sink_node.text:
                self.sink_protocol_info = [p.strip() for p in sink_node.text.split(",") if p.strip()]
                logger.info(f"Renderer reports {len(self.sink_protocol_info)} supported playback format(s).")
        except Exception as e:
            logger.debug(f"GetProtocolInfo not available (not used for anything critical): {e}")

    def select_renderer(self):
        """Interactive console menu to discover and select a renderer. (CLI only --
        the web app calls discover_renderers()/resolve_control_url() directly.)"""
        urls = self.discover_renderers()

        if not urls:
            print(" [!] No active DLNA Renderers detected via SSDP.")
            input("Press Enter to run in browser-only mode (metadata inspector)...")
            return False

        renderers = []
        print("\n[+] Found the following DLNA Renderers:")
        for i, url in enumerate(urls, 1):
            name = self.get_friendly_name(url)
            renderers.append((name, url))
            print(f"  {i}. {name} ({url})")

        while True:
            try:
                choice = input(f"\nSelect a renderer (1-{len(renderers)}) or type 'skip': ").strip()
                if choice.lower() == 'skip':
                    return False
                idx = int(choice) - 1
                if 0 <= idx < len(renderers):
                    self.resolve_control_url(renderers[idx][1])
                    print(f"[+] Selected Renderer: {self.friendly_name}")
                    return True
                print("Invalid choice.")
            except ValueError:
                print("Please enter a valid integer or 'skip'.")

    def play_uri(self, uri, title):
        """Tells the renderer to load and play the selected media URI.
        Returns True/False rather than raising, since a renderer that has
        gone to sleep or been powered off mid-session is a normal event,
        not an application error."""
        if not self.control_url:
            return False

        safe_title = html.escape(title)
        safe_uri = html.escape(uri)

        meta_didl = f"""&lt;DIDL-Lite xmlns="urn:schemas-upnp-org:metadata-1-0/DIDL-Lite/" xmlns:dc="http://purl.org/dc/elements/1.1/" xmlns:upnp="urn:schemas-upnp-org:metadata-1-0/upnp/"&gt;
  &lt;item id="0" parentID="0" restricted="1"&gt;
    &lt;dc:title&gt;{safe_title}&lt;/dc:title&gt;
    &lt;upnp:class&gt;object.item.audioItem.musicTrack&lt;/upnp:class&gt;
    &lt;res&gt;{safe_uri}&lt;/res&gt;
  &lt;/item&gt;
&lt;/DIDL-Lite&gt;"""

        set_uri_soap = f"""<?xml version="1.0" encoding="utf-8"?>
<s:Envelope xmlns:s="{self.NS_SOAP}" s:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/">
  <s:Body>
    <u:SetAVTransportURI xmlns:u="{self.NS_AVT}">
      <InstanceID>0</InstanceID>
      <CurrentURI>{uri}</CurrentURI>
      <CurrentURIMetaData>{meta_didl}</CurrentURIMetaData>
    </u:SetAVTransportURI>
  </s:Body>
</s:Envelope>"""

        play_soap = f"""<?xml version="1.0" encoding="utf-8"?>
<s:Envelope xmlns:s="{self.NS_SOAP}" s:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/">
  <s:Body>
    <u:Play xmlns:u="{self.NS_AVT}">
      <InstanceID>0</InstanceID>
      <Speed>1</Speed>
    </u:Play>
  </s:Body>
</s:Envelope>"""

        headers = {"Content-Type": 'text/xml; charset="utf-8"', "Connection": "close"}
        try:
            headers["SOAPACTION"] = f'"{self.NS_AVT}#SetAVTransportURI"'
            # Bumped from 5s -- some renderers (GGMM in particular) can be
            # slow to acknowledge this right after being told to load a
            # new URI, seemingly while they (or the media server they're
            # pulling from) are still fetching data for it.
            r1 = requests.post(self.control_url, data=set_uri_soap, headers=headers, timeout=10)
            r1.raise_for_status()

            headers["SOAPACTION"] = f'"{self.NS_AVT}#Play"'
            r2 = requests.post(self.control_url, data=play_soap, headers=headers, timeout=10)
            r2.raise_for_status()
            return True
        except Exception as e:
            logger.warning(f"Playback action failed (renderer may be offline): {e}")
            return False

    def pause(self):
        """Sends Pause command to renderer. Returns True/False."""
        if not self.control_url:
            return False
        soap = f"""<?xml version="1.0" encoding="utf-8"?>
<s:Envelope xmlns:s="{self.NS_SOAP}"><s:Body><u:Pause xmlns:u="{self.NS_AVT}"><InstanceID>0</InstanceID></u:Pause></s:Body></s:Envelope>"""
        return self._send_command("Pause", soap)

    def stop(self):
        """Sends Stop command to renderer. Returns True/False."""
        if not self.control_url:
            return False
        soap = f"""<?xml version="1.0" encoding="utf-8"?>
<s:Envelope xmlns:s="{self.NS_SOAP}"><s:Body><u:Stop xmlns:u="{self.NS_AVT}"><InstanceID>0</InstanceID></u:Stop></s:Body></s:Envelope>"""
        return self._send_command("Stop", soap)

    def resume(self):
        """Sends Play command to resume paused playback. Returns True/False."""
        if not self.control_url:
            return False
        soap = f"""<?xml version="1.0" encoding="utf-8"?>
<s:Envelope xmlns:s="{self.NS_SOAP}"><s:Body><u:Play xmlns:u="{self.NS_AVT}"><InstanceID>0</InstanceID><Speed>1</Speed></u:Play></s:Body></s:Envelope>"""
        return self._send_command("Play", soap)

    def get_transport_state(self):
        """Queries current state (PLAYING, STOPPED, PAUSED_PLAYBACK, etc.).
        Returns "UNKNOWN" when the renderer can't be reached -- this is the
        signal the web app's connectivity monitor watches for."""
        if not self.control_url:
            return "NO_MEDIA_PRESENT"
        soap = f"""<?xml version="1.0" encoding="utf-8"?>
<s:Envelope xmlns:s="{self.NS_SOAP}">
  <s:Body>
    <u:GetTransportInfo xmlns:u="{self.NS_AVT}">
      <InstanceID>0</InstanceID>
    </u:GetTransportInfo>
  </s:Body>
</s:Envelope>"""
        headers = {
            "Content-Type": 'text/xml; charset="utf-8"',
            "SOAPACTION": f'"{self.NS_AVT}#GetTransportInfo"',
            "Connection": "close"
        }
        try:
            # Bumped from 2s -- this is also what the new stopped-
            # notification probe calls (see PlaybackSession.
            # handle_stopped_notification()), right when the renderer may
            # still be busy settling in from a track change.
            r = requests.post(self.control_url, data=soap, headers=headers, timeout=6)
            r.raise_for_status()
            root = ET.fromstring(r.content)
            state_node = root.find(".//CurrentTransportState")
            if state_node is not None:
                return state_node.text
        except Exception as e:
            logger.debug(f"get_transport_state failed (renderer likely offline): {e}")
        return "UNKNOWN"

    def get_position_info(self):
        """Queries the renderer for current track duration, position, and metadata."""
        if not self.control_url:
            return {"title": "None", "artist": None, "duration": "00:00:00", "position": "00:00:00"}

        soap = f"""<?xml version="1.0" encoding="utf-8"?>
<s:Envelope xmlns:s="{self.NS_SOAP}">
  <s:Body>
    <u:GetPositionInfo xmlns:u="{self.NS_AVT}">
      <InstanceID>0</InstanceID>
    </u:GetPositionInfo>
  </s:Body>
</s:Envelope>"""

        headers = {
            "Content-Type": 'text/xml; charset="utf-8"',
            "SOAPACTION": f'"{self.NS_AVT}#GetPositionInfo"',
            "Connection": "close"
        }

        try:
            # Bumped from 2s -- same "renderer/media-server briefly slow
            # right after a track change" reasoning as get_transport_state().
            r = requests.post(self.control_url, data=soap, headers=headers, timeout=6)
            r.raise_for_status()
            root = ET.fromstring(r.content)

            duration = root.find(".//TrackDuration").text or "00:00:00"
            position = root.find(".//RelTime").text or "00:00:00"

            title = "Unknown"
            artist = None
            meta_xml = root.find(".//TrackMetaData").text
            if meta_xml and meta_xml != "NOT_IMPLEMENTED":
                try:
                    meta_root = ET.fromstring(meta_xml)
                    ns = {
                        'dc': 'http://purl.org/dc/elements/1.1/',
                        'upnp': 'urn:schemas-upnp-org:metadata-1-0/upnp/',
                    }
                    title_node = meta_root.find(".//dc:title", ns)
                    if title_node is not None:
                        title = title_node.text
                    # upnp:artist is the standard performer tag; some
                    # servers/renderers only populate dc:creator instead
                    # (which is really "author", but in practice gets
                    # used as a stand-in for artist on music items), so
                    # fall back to that when upnp:artist is absent.
                    artist_node = meta_root.find(".//upnp:artist", ns)
                    if artist_node is None:
                        artist_node = meta_root.find(".//dc:creator", ns)
                    if artist_node is not None and artist_node.text:
                        artist = artist_node.text
                except Exception:
                    pass

            return {"title": title, "artist": artist, "duration": duration, "position": position}
        except Exception as e:
            logger.debug(f"get_position_info failed (renderer likely offline): {e}")
            return {"title": "None", "artist": None, "duration": "00:00:00", "position": "00:00:00"}

    # -- RenderingControl (volume) --------------------------------------
    #
    # Not every renderer implements this the same way AVTransport is
    # implemented practically everywhere: some skip RenderingControl
    # entirely, and at least one real-world device tested against this
    # app (an LG WebOS TV) implements the service but deliberately
    # rejects the volume actions with UPnPError 606 "Action not
    # authorized" -- a vendor policy choice, not a bug on either end.
    #
    # There's no separate "do you support this" query in the UPnP spec,
    # so get_volume() returning None doubles as both "unreachable" and
    # "not supported" -- callers (state.py) treat both the same way:
    # no volume control for this renderer, not an error to surface.

    def get_volume(self, channel="Master"):
        """Returns current volume 0-100, or None if unsupported/unreachable."""
        if not self.rendering_control_url:
            return None
        soap = f"""<?xml version="1.0" encoding="utf-8"?>
<s:Envelope xmlns:s="{self.NS_SOAP}">
  <s:Body>
    <u:GetVolume xmlns:u="{self.NS_RC}">
      <InstanceID>0</InstanceID>
      <Channel>{channel}</Channel>
    </u:GetVolume>
  </s:Body>
</s:Envelope>"""
        headers = {
            "Content-Type": 'text/xml; charset="utf-8"',
            "SOAPACTION": f'"{self.NS_RC}#GetVolume"',
            "Connection": "close"
        }
        try:
            r = requests.post(self.rendering_control_url, data=soap, headers=headers, timeout=3)
            r.raise_for_status()
            root = ET.fromstring(r.content)
            vol_node = root.find(".//CurrentVolume")
            if vol_node is not None and vol_node.text is not None:
                return int(vol_node.text)
        except Exception as e:
            logger.debug(f"get_volume failed (renderer may not support volume control): {e}")
        return None

    def set_volume(self, value, channel="Master"):
        """Sets volume 0-100 (clamped). Returns True/False -- same pattern
        as _send_command; a rejected or failed call is an expected
        outcome here, not an exceptional one."""
        if not self.rendering_control_url:
            return False
        value = max(0, min(100, int(value)))
        soap = f"""<?xml version="1.0" encoding="utf-8"?>
<s:Envelope xmlns:s="{self.NS_SOAP}">
  <s:Body>
    <u:SetVolume xmlns:u="{self.NS_RC}">
      <InstanceID>0</InstanceID>
      <Channel>{channel}</Channel>
      <DesiredVolume>{value}</DesiredVolume>
    </u:SetVolume>
  </s:Body>
</s:Envelope>"""
        headers = {
            "Content-Type": 'text/xml; charset="utf-8"',
            "SOAPACTION": f'"{self.NS_RC}#SetVolume"',
            "Connection": "close"
        }
        try:
            r = requests.post(self.rendering_control_url, data=soap, headers=headers, timeout=3)
            r.raise_for_status()
            return True
        except Exception as e:
            logger.debug(f"set_volume failed (renderer may not support volume control): {e}")
            return False

    def _send_command(self, action, soap_payload):
        headers = {
            "Content-Type": 'text/xml; charset="utf-8"',
            "SOAPACTION": f'"{self.NS_AVT}#{action}"',
            "Connection": "close"
        }
        try:
            r = requests.post(self.control_url, data=soap_payload, headers=headers, timeout=3)
            r.raise_for_status()
            return True
        except Exception as e:
            logger.warning(f"{action} command failed (renderer may be offline): {e}")
            return False
