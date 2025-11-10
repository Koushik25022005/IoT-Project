from coapthon.server.coap import CoAP
from coapthon.resources.resource import Resource
import json
import time
from threading import Lock
import sqlite3
from datetime import datetime

class ParkingResource(Resource):
    def __init__(self, name="ParkingResource", coap_server=None):
        super(ParkingResource, self).__init__(name, coap_server)
        self.parking_status = {
            "slot1": {"occupied": False, "last_updated": 0, "car_count": 0},
            "slot2": {"occupied": False, "last_updated": 0, "car_count": 0},
            "slot3": {"occupied": False, "last_updated": 0, "car_count": 0},
            "slot4": {"occupied": False, "last_updated": 0, "car_count": 0}
        }
        self.lock = Lock()
        self.setup_database()
        
    def setup_database(self):
        """Initialize SQLite database for logging"""
        self.conn = sqlite3.connect('parking.db', check_same_thread=False)
        cursor = self.conn.cursor()
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS parking_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                slot_id TEXT NOT NULL,
                event_type TEXT NOT NULL,
                timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS parking_stats (
                slot_id TEXT PRIMARY KEY,
                total_cars INTEGER DEFAULT 0,
                last_updated DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        self.conn.commit()
        
    def log_event(self, slot_id, event_type):
        """Log parking events to database"""
        cursor = self.conn.cursor()
        cursor.execute(
            "INSERT INTO parking_events (slot_id, event_type) VALUES (?, ?)",
            (slot_id, event_type)
        )
        
        # Update statistics
        if event_type == "car_entered":
            cursor.execute('''
                INSERT OR REPLACE INTO parking_stats (slot_id, total_cars, last_updated)
                VALUES (?, COALESCE((SELECT total_cars FROM parking_stats WHERE slot_id = ?), 0) + 1, CURRENT_TIMESTAMP)
            ''', (slot_id, slot_id))
            
        self.conn.commit()
        
    def render_GET(self, request):
        """Handle GET request - return current parking status"""
        with self.lock:
            response = self._init_response(request)
            # Add available slots count
            available_slots = sum(1 for slot in self.parking_status.values() if not slot["occupied"])
            status_with_meta = {
                "parking_status": self.parking_status,
                "available_slots": available_slots,
                "total_slots": len(self.parking_status)
            }
            response.payload = json.dumps(status_with_meta)
        return response

    def render_PUT(self, request):
        """Handle PUT request - update parking slot status"""
        try:
            data = json.loads(request.payload)
            slot_id = data.get("slot_id")
            occupied = data.get("occupied")
            
            if slot_id in self.parking_status and isinstance(occupied, bool):
                with self.lock:
                    previous_state = self.parking_status[slot_id]["occupied"]
                    self.parking_status[slot_id]["occupied"] = occupied
                    self.parking_status[slot_id]["last_updated"] = time.time()
                    
                    # Log the event
                    if occupied and not previous_state:
                        self.parking_status[slot_id]["car_count"] += 1
                        self.log_event(slot_id, "car_entered")
                        print(f"Car entered {slot_id}")
                    elif not occupied and previous_state:
                        self.log_event(slot_id, "car_exited")
                        print(f"Car exited {slot_id}")
                    
                    response = self._init_response(request)
                    response_data = {
                        "status": "success", 
                        "slot": slot_id, 
                        "occupied": occupied,
                        "car_count": self.parking_status[slot_id]["car_count"]
                    }
                    response.payload = json.dumps(response_data)
            else:
                response = self._init_response(request)
                response.payload = json.dumps({"status": "error", "message": "Invalid data"})
                response.code = 400
                
        except Exception as e:
            response = self._init_response(request)
            response.payload = json.dumps({"status": "error", "message": str(e)})
            response.code = 400
            
        return response

class CoAPParkingServer(CoAP):
    def __init__(self, host, port):
        CoAP.__init__(self, (host, port))
        self.add_resource('parking/', ParkingResource())

def main():
    server = CoAPParkingServer("0.0.0.0", 5683)
    print("CoAP Parking Server started on coap://0.0.0.0:5683/parking/")
    try:
        server.listen(10)
    except KeyboardInterrupt:
        print("Server Shutdown")
        server.close()
        print("Exiting...")

if __name__ == '__main__':
    main()