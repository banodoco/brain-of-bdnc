"""
Lightweight HTTP health check server for Railway deployment monitoring.
Runs on a separate thread to not block the Discord bot.
"""
import threading
import logging
from http.server import HTTPServer, BaseHTTPRequestHandler
import json
from datetime import datetime
import os

class HealthCheckHandler(BaseHTTPRequestHandler):
    """Handler for health check HTTP requests"""
    
    # Class-level variables to track bot state
    bot_ready = False
    startup_time = None
    last_heartbeat = None
    deployment_id = os.getenv('RAILWAY_DEPLOYMENT_ID', 'unknown')
    service_id = os.getenv('RAILWAY_SERVICE_ID', 'unknown')
    
    def do_GET(self):
        """Handle GET requests"""
        if self.path == '/health':
            self.handle_health()
        elif self.path == '/ready':
            self.handle_ready()
        elif self.path == '/status':
            self.handle_status()
        else:
            self.send_error(404, "Not Found")
    
    def handle_health(self):
        """Basic health check - always returns 200 if server is running"""
        self.send_response(200)
        self.send_header('Content-type', 'application/json')
        self.end_headers()
        response = {
            'status': 'healthy',
            'deployment_id': self.deployment_id,
            'timestamp': datetime.utcnow().isoformat()
        }
        self.wfile.write(json.dumps(response).encode())
    
    def handle_ready(self):
        """Readiness check - returns 200 only when bot is ready"""
        if self.bot_ready:
            self.send_response(200)
            self.send_header('Content-type', 'application/json')
            self.end_headers()
            response = {
                'status': 'ready',
                'deployment_id': self.deployment_id,
                'startup_time': self.startup_time.isoformat() if self.startup_time else None,
                'timestamp': datetime.utcnow().isoformat()
            }
            self.wfile.write(json.dumps(response).encode())
        else:
            self.send_response(503)
            self.send_header('Content-type', 'application/json')
            self.end_headers()
            response = {
                'status': 'not_ready',
                'deployment_id': self.deployment_id,
                'timestamp': datetime.utcnow().isoformat()
            }
            self.wfile.write(json.dumps(response).encode())
    
    def handle_status(self):
        """Detailed status check with all available metrics"""
        uptime = None
        if self.startup_time:
            uptime = (datetime.utcnow() - self.startup_time).total_seconds()
        
        self.send_response(200)
        self.send_header('Content-type', 'application/json')
        self.end_headers()
        response = {
            'status': 'ready' if self.bot_ready else 'starting',
            'deployment_id': self.deployment_id,
            'service_id': self.service_id,
            'startup_time': self.startup_time.isoformat() if self.startup_time else None,
            'uptime_seconds': uptime,
            'last_heartbeat': self.last_heartbeat.isoformat() if self.last_heartbeat else None,
            'timestamp': datetime.utcnow().isoformat()
        }
        self.wfile.write(json.dumps(response).encode())
    
    def log_message(self, format, *args):
        """Suppress default HTTP server logging"""
        pass

class HealthServer:
    """Health check server that runs in a background thread"""
    
    def __init__(self, port=8080):
        self.port = port
        self.server = None
        self.thread = None
        self.logger = logging.getLogger('DiscordBot')
        self._started = False
        
    def start(self):
        """Start the health check server in a background thread"""
        if self._started:
            self.logger.warning("Health server already started")
            return
        
        try:
            self.server = HTTPServer(('0.0.0.0', self.port), HealthCheckHandler)
            self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
            self.thread.start()
            self._started = True
            self.logger.info(f"Health check server started on port {self.port}")
            self.logger.info(f"  - /health  : Basic liveness probe")
            self.logger.info(f"  - /ready   : Readiness probe (200 when bot ready)")
            self.logger.info(f"  - /status  : Detailed status with metrics")
        except Exception as e:
            self.logger.error(f"Failed to start health check server: {e}")
    
    def mark_ready(self):
        """Mark the bot as ready"""
        HealthCheckHandler.bot_ready = True
        HealthCheckHandler.startup_time = datetime.utcnow()
        self.logger.info("Bot marked as ready in health server")
    
    def update_heartbeat(self):
        """Update the last heartbeat timestamp"""
        HealthCheckHandler.last_heartbeat = datetime.utcnow()
    
    def stop(self):
        """Stop the health check server"""
        if self.server:
            self.server.shutdown()
            self.logger.info("Health check server stopped")
