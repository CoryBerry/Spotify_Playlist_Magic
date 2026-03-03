# Yes, this is a lot of boilerplate just to show today's date.
# The alternative is JavaScript in the HTML, which does it in one line.
# But here we are — serving a full HTTP server for a timestamp.

from http.server import HTTPServer, SimpleHTTPRequestHandler
from datetime import datetime

# Extend the built-in request handler so we can intercept GET requests
class Handler(SimpleHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/":
            # Get today's date and format it as dd, mm, yyyy
            date_str = datetime.now().strftime("%d, %m, %Y")

            # Read the HTML template from disk
            with open("index.html", "r") as f:
                html = f.read()

            # Swap the placeholder with the real date
            html = html.replace("{{DATE}}", date_str)

            # Send the response
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(html.encode())
        else:
            # Fall back to default static file serving for anything else
            super().do_GET()

if __name__ == "__main__":
    server = HTTPServer(("localhost", 8000), Handler)
    print("Serving at http://localhost:8000")
    server.serve_forever()
