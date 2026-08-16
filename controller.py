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
import time

class Controller:
    """The root command-loop coordinator."""
    def __init__(self, queue, browser):
        self.queue = queue
        self.browser = browser

    def print_help(self):
        """Displays available interactive commands."""
        print("\n" + "🎧" * 25)
        print(f" ACTIVE OUTPUT: {self.queue.renderer.friendly_name} ({self.queue.renderer.host})")
        print(" " + "•" * 48)
        print(" Command options:")
        print("   b : Browse Media")
        print("   q : Show Play Queue")
        print("   s : Show Current Status (Snapshot)")
        print("   m : Monitor Live Playback Progress")
        print("   + : Skip (Next Track)")
        print("   - : Previous Track")
        print("   p : Play / Pause")
        print("   x  : Stop")
        print("   c  : Clear Queue")
        print("   S  : Save Playlist")
        print("   L  : Load Playlist")
        print("   h  : Show Help Menu")
        print("   Q  : Exit Controller")
        print("🎧" * 25)

    def show_status(self):
        """Fetches and displays a clean status snapshot."""
        state = self.queue.renderer.get_transport_state()
        pos_info = self.queue.renderer.get_position_info()
        
        print("\n" + "─" * 50)
        print(f" 🎛️  RENDERER STATUS: {self.queue.renderer.friendly_name} ({self.queue.renderer.host})")
        print(f" 🚦 Transport State : {state}")
        print(f" 🎵 Current Track   : {pos_info['title']}")
        print(f" ⏱️  Playback Time   : {pos_info['position']} / {pos_info['duration']}")
        print("" + "─" * 50)

    def run_monitor(self):
        """Loops continuously to show active live progress until user interrupts."""
        print("\n[+] Entering Live Monitor Mode. Press Ctrl+C to stop monitoring and return to menu.\n")
        try:
            while True:
                state = self.queue.renderer.get_transport_state()
                pos_info = self.queue.renderer.get_position_info()
                
                # Parse strings to display a text progress bar (HH:MM:SS -> seconds)
                def to_seconds(t_str):
                    try:
                        parts = list(map(int, t_str.split(':')))
                        return parts[0]*3600 + parts[1]*60 + parts[2]
                    except Exception:
                        return 0

                cur_sec = to_seconds(pos_info['position'])
                tot_sec = to_seconds(pos_info['duration'])
                
                bar_width = 30
                if tot_sec > 0:
                    pct = cur_sec / tot_sec
                    filled = int(bar_width * pct)
                else:
                    pct = 0.0
                    filled = 0
                
                prog_bar = "█" * filled + "░" * (bar_width - filled)
                
                print(f"\r [{state}] {pos_info['title'][:25]} |{prog_bar}| {pos_info['position']}/{pos_info['duration']}", end="", flush=True)
                time.sleep(1.0)
                
        except KeyboardInterrupt:
            print("\n\n[+] Exited Monitor Mode.")

    def run(self):
        self.queue.start_monitoring()

        # Print help layout strictly on first startup
        self.print_help()
        
        while True:
            current = self.queue.get_current_track()
            track_title = current['title'] if current else "None"
            
            # Simple, non-intrusive running status header
            # print(f"\n[OUTPUT: {self.queue.renderer.friendly_name} ({self.queue.renderer.host}) | TRACK: {track_title}]")
            cmd = input("Enter command (or 'h' for help): ").strip()
            
            if cmd == 'b':
                self.browser.start_ui(self.queue)
            elif cmd == 'q':
                self.queue.display_queue()
            elif cmd == 's' or len(cmd) == 0:
                self.show_status()
            elif cmd == 'm':
                self.run_monitor()
            elif cmd == '+':
                self.queue.next()
            elif cmd == '-':
                self.queue.prev()
            elif cmd == 'p':
                self.queue.toggle_play()
            elif cmd == 'x':
                self.queue.stop()
            elif cmd == 'c':
                self.queue.clear()
            elif cmd == 'h':
                self.print_help()
            elif cmd == 'S':  # Save Playlist command option
                name = input("Enter playlist name: ").strip()
                if len(name) > 0:
                    self.queue.save_playlist(name)
                else:
                    print("Nothing saved.")
            elif cmd == 'L':  # Load Playlist command option
                name = input("Enter playlist name to load: ").strip()
                if len(name) > 0:
                    self.queue.load_playlist(name)
                else:
                    print("Load canceled.")
            elif cmd == 'Q':
                print("Shutting down controller...")
                self.queue.shutdown()
                break
            else:
                print("[!] Unknown command. Type 'h' for options.")
