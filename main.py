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
from dlnarenderer import DLNARenderer
from dlnabrowser import DLNABrowser
from playqueue import PlayQueue
from controller import Controller

def main():
    renderer = DLNARenderer()
    browser = DLNABrowser()
    
    try:
        # Step 1: Discover / Select Renderer Output
        renderer.select_renderer()
        print("")
        
        # Step 2: Initialize Play Queue Threading Engine
        queue = PlayQueue(renderer)
        
        # Step 3: Discover / Select Media Server
        if browser.select_server():
            print(f"[+] Active Control URL: {browser.control_url}\n")
            
            # Step 4: Run controller loop
            controller = Controller(queue, browser)
            controller.run()
        else:
            queue.shutdown()
            print("Exiting.")
            
    except Exception as e:
        print(f"\n[!] Global error: {e}")

if __name__ == "__main__":
    main()
