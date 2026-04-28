#include <Arduino.h>
#include <ESP8266WiFi.h>
#include <ESP8266HTTPClient.h>
#include <WiFiClient.h>
#include <Wire.h>
#include <Adafruit_GFX.h>
#include <Adafruit_SSD1306.h>

// ============================================================
// Settings
// ============================================================

#define SCREEN_WIDTH 128
#define SCREEN_HEIGHT 64
Adafruit_SSD1306 display(SCREEN_WIDTH, SCREEN_HEIGHT, &Wire, -1);

static const char* WIFI_SSID     = "A1-FT34-B1EC18-2.4G"; // свой wifi
static const char* WIFI_PASSWORD = "838sS3Hq";

static const char* PROXY_HOST = "192.168.1.8";
static const int   PROXY_PORT = 8080;

static const unsigned long UPDATE_INTERVAL_MS = 20000;

// ============================================================
// State
// ============================================================

struct BusInfo {
  char id[12];
  int  distM;
  int  minsEta;
};

BusInfo g_buses[4];
int     g_busCount   = 0;
char    g_official[32] = "";
char    g_stopName[32] = "";
char    g_statusMsg[32] = "Starting...";
bool    g_hasData    = false;

unsigned long g_lastUpdate = 0;

// ============================================================
// Display
// ============================================================

void drawStatus() {
  display.clearDisplay();
  display.setTextSize(1);
  display.setTextColor(SSD1306_WHITE);
  display.setCursor(0, 0);
  display.print("minsktrans.by");
  display.drawFastHLine(0, 12, 128, SSD1306_WHITE);
  display.setCursor(0, 20);
  display.print(g_statusMsg);
  display.display();
}

void drawData() {
  display.clearDisplay();
  display.setTextSize(1);
  display.setTextColor(SSD1306_WHITE);

  // Header
  display.setCursor(0, 0);
  display.print(g_stopName);
  display.drawFastHLine(0, 10, 128, SSD1306_WHITE);

  if (!g_hasData || g_busCount == 0) {
    display.setCursor(0, 28);
    display.print("No buses nearby");
  } else {
    for (int i = 0; i < g_busCount; i++) {
      char line[24];
      snprintf(line, sizeof(line), "#%-6s %5dm ~%2dmin",
               g_buses[i].id,
               g_buses[i].distM,
               g_buses[i].minsEta);
      display.setCursor(0, 14 + i * 10);
      display.print(line);
    }
  }

  // Bottom: official forecast
  display.drawFastHLine(0, 54, 128, SSD1306_WHITE);
  display.setCursor(0, 56);
  if (strlen(g_official) == 0) {
    display.print("official: n/a");
  } else {
    char buf[24];
    snprintf(buf, sizeof(buf), "ETA: %s min", g_official);
    display.print(buf);
  }

  display.display();
}

// ============================================================
// Minimal JSON parser
// ============================================================

bool jsonGetStr(const String& json, const char* key, char* buf, int bufLen) {
  String searchKey = String("\"") + key + "\":";  // без кавычки после
  int pos = json.indexOf(searchKey);
  if (pos < 0) return false;
  pos += searchKey.length();
  // пропускаем пробелы между : и "
  while (pos < (int)json.length() && json[pos] == ' ') pos++;
  if (json[pos] != '"') return false;
  pos++; // пропускаем открывающую кавычку
  int end = json.indexOf('"', pos);
  if (end < 0) return false;
  int len = min(end - pos, bufLen - 1);
  json.substring(pos, pos + len).toCharArray(buf, bufLen);
  return true;
}

int jsonGetInt(const String& json, const char* key, int def = 0) {
  String searchKey = String("\"") + key + "\":";
  int pos = json.indexOf(searchKey);
  if (pos < 0) return def;
  pos += searchKey.length();
  while (pos < (int)json.length() && json[pos] == ' ') pos++;
  return json.substring(pos).toInt();
}

// ============================================================
// Fetch data from proxy
// ============================================================

bool fetchData() {
  snprintf(g_statusMsg, sizeof(g_statusMsg), "Fetching...");
  drawStatus();

  WiFiClient client;
  HTTPClient http;

  String url = String("http://") + PROXY_HOST + ":" + PROXY_PORT + "/buses";
  if (!http.begin(client, url)) {
    snprintf(g_statusMsg, sizeof(g_statusMsg), "http.begin ERR");
    return false;
  }

  int code = http.GET();
  Serial.printf("GET /buses -> %d, heap: %d\n", code, ESP.getFreeHeap());

  if (code != 200) {
    snprintf(g_statusMsg, sizeof(g_statusMsg), "HTTP %d", code);
    http.end();
    return false;
  }

  String body = http.getString();
  http.end();

  Serial.printf("Body len: %d\n", body.length());

  if (!jsonGetStr(body, "stop", g_stopName, sizeof(g_stopName))) {
    snprintf(g_statusMsg, sizeof(g_statusMsg), "Parse ERR");
    Serial.printf("Body: %s\n", body.c_str());
    Serial.printf("busCount: %d\n", g_busCount);
    Serial.printf("stopName: %s\n", g_stopName);
    Serial.printf("official: %s\n", g_official);
    return false;
  }

  jsonGetStr(body, "official", g_official, sizeof(g_official));

  g_busCount = 0;
  int pos = body.indexOf("\"buses\":");
  if (pos >= 0) {
    pos = body.indexOf('[', pos) + 1;
    while (g_busCount < 4) {
      int objStart = body.indexOf('{', pos);
      int objEnd   = body.indexOf('}', objStart);
      if (objStart < 0 || objEnd < 0) break;

      String obj = body.substring(objStart, objEnd + 1);

      // id
      int idPos = obj.indexOf("\"id\":");
      if (idPos >= 0) {
        idPos = obj.indexOf('"', idPos + 5) + 1;
        int idEnd = obj.indexOf('"', idPos);
        obj.substring(idPos, idEnd).toCharArray(g_buses[g_busCount].id, sizeof(g_buses[0].id));
      }

      // dist
      int distPos = obj.indexOf("\"dist\":");
      if (distPos >= 0) {
        distPos += 7;
        while (obj[distPos] == ' ') distPos++;
        g_buses[g_busCount].distM = obj.substring(distPos).toInt();
      }

      // eta
      int etaPos = obj.indexOf("\"eta\":");
      if (etaPos >= 0) {
        etaPos += 6;
        while (obj[etaPos] == ' ') etaPos++;
        g_buses[g_busCount].minsEta = obj.substring(etaPos).toInt();
      }

      g_busCount++;
      pos = objEnd + 1;

      if (body.indexOf('{', pos) > body.indexOf(']', pos)) break;
    }
  }

  g_hasData = true;
  return true;
}

// ============================================================
// setup / loop
// ============================================================

void setup() {
  Serial.begin(9600);
  Serial.println("\n=== minsktrans bus display ===");

  if(!display.begin(SSD1306_SWITCHCAPVCC, 0x3C)) { 
        for(;;);
    }
  display.clearDisplay();
  display.display();

  snprintf(g_statusMsg, sizeof(g_statusMsg), "Connecting WiFi");
  drawStatus();

  WiFi.begin(WIFI_SSID, WIFI_PASSWORD);
  int tries = 0;
  while (WiFi.status() != WL_CONNECTED && tries < 30) {
    delay(500);
    Serial.print(".");
    tries++;
  }
  if (WiFi.status() != WL_CONNECTED) {
    snprintf(g_statusMsg, sizeof(g_statusMsg), "No WiFi!");
    drawStatus();
    while (true) delay(1000);
  }
  Serial.printf("\nWiFi: %s\n", WiFi.localIP().toString().c_str());

  fetchData();
  drawData();
}

void loop() {
  if (millis() - g_lastUpdate >= UPDATE_INTERVAL_MS) {
    g_lastUpdate = millis();
    if (fetchData()) {
      drawData();
    } else {
      drawStatus();
    }
  }
}