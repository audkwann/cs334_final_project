// BTS7960 serial motor control for Arduino UNO
// Commands over USB serial:
//   1 -> spin left
//   2 -> spin right
//   3 -> soft stop
//   p -> report encoder position
//   z -> zero encoder (set current position as home)
//   g<ticks> -> go to absolute encoder position (e.g. g1425)
//   c<R>,<G>,<B> -> set NeoPixel strip color (e.g. c255,40,0)
//   b0 / b1    -> disable / enable breathing animation

#include <Adafruit_NeoPixel.h>

#define NEOPIXEL_PIN 4
#define NUM_LEDS 59

Adafruit_NeoPixel strip(NUM_LEDS, NEOPIXEL_PIN, NEO_GRBW + NEO_KHZ800);

const int RPWM = 5;
const int LPWM = 6;
const int REN = 7;
const int LEN = 8;

// Encoder pins (hardware interrupts on UNO)
const int ENCODER_A_PIN = 2;
const int ENCODER_B_PIN = 3;

const long BAUD_RATE = 115200;
const int CRUISE_PWM = 120;     // manual left/right speed (slow for testing)
const int GOTO_PWM = 100;       // go-to-position speed
const int GOTO_SLOW_PWM = 60;   // even slower when close to target
const int PWM_STEP = 5;
const unsigned long RAMP_INTERVAL_MS = 10;
const unsigned long POS_REPORT_INTERVAL_MS = 100;
const bool LEFT_IS_RPWM = false;

// Encoder constants
const long TICKS_PER_REV = 16567;  // 1425.1 PPR * 11.625 belt ratio
const long POSITION_DEADBAND = 20; // ticks tolerance (~0.4 degrees)
const long SLOW_ZONE = 500;        // start slowing down within this many ticks

// Encoder state (volatile for ISR access)
volatile long encoderCount = 0;

// Go-to-position state
bool gotoActive = false;
long targetPosition = 0;

// LED breathing state
uint8_t baseR = 0, baseG = 0, baseB = 0;
bool breatheEnabled = true;
unsigned long breatheStartMs = 0;  // reset on color change so cycle starts at peak
const unsigned long BREATHE_PERIOD_MS = 3000;  // full cycle duration
const uint8_t BREATHE_MIN = 40;   // minimum brightness (out of 255)
const uint8_t BREATHE_MAX = 255;  // maximum brightness

enum Direction {
  DIR_STOPPED,
  DIR_LEFT,
  DIR_RIGHT
};

Direction activeDirection = DIR_STOPPED;
Direction requestedDirection = DIR_STOPPED;
int currentPwm = 0;
unsigned long lastRampUpdateMs = 0;
unsigned long lastPosReportMs = 0;

// --- Encoder ISRs ---
void encoderISR_A() {
  if (digitalRead(ENCODER_B_PIN) == digitalRead(ENCODER_A_PIN)) {
    encoderCount--;
  } else {
    encoderCount++;
  }
}

void encoderISR_B() {
  if (digitalRead(ENCODER_A_PIN) == digitalRead(ENCODER_B_PIN)) {
    encoderCount++;
  } else {
    encoderCount--;
  }
}

// --- Motor helpers ---
void stopMotor() {
  analogWrite(RPWM, 0);
  analogWrite(LPWM, 0);
}

void setLeftPwm(int pwm) {
  pwm = constrain(pwm, 0, 255);

  if (LEFT_IS_RPWM) {
    analogWrite(LPWM, 0);
    analogWrite(RPWM, pwm);
  } else {
    analogWrite(RPWM, 0);
    analogWrite(LPWM, pwm);
  }
}

void setRightPwm(int pwm) {
  pwm = constrain(pwm, 0, 255);

  if (LEFT_IS_RPWM) {
    analogWrite(RPWM, 0);
    analogWrite(LPWM, pwm);
  } else {
    analogWrite(LPWM, 0);
    analogWrite(RPWM, pwm);
  }
}

void applyMotorOutput(Direction direction, int pwm) {
  if (direction == DIR_LEFT) {
    setLeftPwm(pwm);
  } else if (direction == DIR_RIGHT) {
    setRightPwm(pwm);
  } else {
    stopMotor();
  }
}

// --- Serial command parsing ---
char cmdBuffer[16];
int cmdBufferIdx = 0;

void handleCommand(char command) {
  if (command == '1') {
    gotoActive = false;
    requestedDirection = DIR_LEFT;
    Serial.println("CMD LEFT");
  } else if (command == '2') {
    gotoActive = false;
    requestedDirection = DIR_RIGHT;
    Serial.println("CMD RIGHT");
  } else if (command == '3') {
    gotoActive = false;
    requestedDirection = DIR_STOPPED;
    Serial.println("CMD STOP");
  } else if (command == 'p') {
    long pos = encoderCount;
    Serial.print("POS ");
    Serial.println(pos);
  } else if (command == 'z') {
    encoderCount = 0;
    Serial.println("POS 0");
    Serial.println("ZEROED");
  } else {
    Serial.print("IGNORED ");
    Serial.println(command);
  }
}

void handleGotoCommand(long ticks) {
  targetPosition = ticks;
  gotoActive = true;
  Serial.print("GOTO ");
  Serial.println(targetPosition);
}

void readSerialCommands() {
  while (Serial.available() > 0) {
    char incoming = Serial.read();

    if (incoming == '\n' || incoming == '\r') {
      if (cmdBufferIdx > 0) {
        cmdBuffer[cmdBufferIdx] = '\0';
        if (cmdBuffer[0] == 'g') {
          long ticks = atol(&cmdBuffer[1]);
          handleGotoCommand(ticks);
        } else if (cmdBuffer[0] == 'c') {
          // Parse c<R>,<G>,<B>
          int r = 0, g = 0, b = 0;
          char *ptr = &cmdBuffer[1];
          r = atoi(ptr);
          ptr = strchr(ptr, ',');
          if (ptr) { ptr++; g = atoi(ptr); ptr = strchr(ptr, ','); }
          if (ptr) { ptr++; b = atoi(ptr); }
          baseR = r; baseG = g; baseB = b;
          breatheStartMs = millis();  // reset cycle so it starts at peak brightness
          strip.fill(strip.Color(r, g, b));
          strip.show();
          Serial.print("LED ");
          Serial.print(r); Serial.print(",");
          Serial.print(g); Serial.print(",");
          Serial.println(b);
        } else if (cmdBuffer[0] == 'b') {
          // b0 = breathing off (solid), b1 = breathing on
          breatheEnabled = (cmdBuffer[1] != '0');
          Serial.print("BREATHE ");
          Serial.println(breatheEnabled ? "ON" : "OFF");
        } else if (cmdBufferIdx == 1) {
          handleCommand(cmdBuffer[0]);
        }
        cmdBufferIdx = 0;
      }
      continue;
    }

    if (incoming == ' ' || incoming == '\t') {
      continue;
    }

    if (cmdBufferIdx < (int)(sizeof(cmdBuffer) - 1)) {
      cmdBuffer[cmdBufferIdx++] = incoming;
    }
  }
}

// --- Go-to-position logic ---
void updateGoto() {
  if (!gotoActive) return;

  long pos = encoderCount;
  long error = targetPosition - pos;

  if (abs(error) <= POSITION_DEADBAND) {
    // Target reached
    gotoActive = false;
    requestedDirection = DIR_STOPPED;
    Serial.print("REACHED ");
    Serial.println(pos);
    return;
  }

  // Determine direction
  Direction needed = (error > 0) ? DIR_LEFT : DIR_RIGHT;

  // Determine speed based on distance
  int targetPwm;
  if (abs(error) < SLOW_ZONE) {
    targetPwm = GOTO_SLOW_PWM;
  } else {
    targetPwm = GOTO_PWM;
  }

  requestedDirection = needed;

  // Override cruise PWM for goto moves
  if (activeDirection == needed && currentPwm > targetPwm) {
    currentPwm = targetPwm;
    applyMotorOutput(activeDirection, currentPwm);
  }
}

// --- Motion ramp ---
void stepTowardStop() {
  if (activeDirection == DIR_STOPPED) {
    currentPwm = 0;
    stopMotor();
    return;
  }

  if (currentPwm > 0) {
    currentPwm = max(0, currentPwm - PWM_STEP);
    applyMotorOutput(activeDirection, currentPwm);
  }

  if (currentPwm == 0) {
    stopMotor();
    activeDirection = DIR_STOPPED;
    Serial.println("STATE STOPPED");
  }
}

void updateMotion() {
  unsigned long now = millis();
  if (now - lastRampUpdateMs < RAMP_INTERVAL_MS) {
    return;
  }
  lastRampUpdateMs = now;

  if (requestedDirection == DIR_STOPPED) {
    stepTowardStop();
    return;
  }

  if (activeDirection == DIR_STOPPED) {
    activeDirection = requestedDirection;
  }

  if (activeDirection != requestedDirection) {
    stepTowardStop();
    return;
  }

  // Determine target PWM (lower for goto moves)
  int targetCruise = gotoActive ? GOTO_PWM : CRUISE_PWM;

  if (currentPwm < targetCruise) {
    currentPwm = min(targetCruise, currentPwm + PWM_STEP);
    applyMotorOutput(activeDirection, currentPwm);
  } else if (currentPwm > targetCruise) {
    currentPwm = max(targetCruise, currentPwm - PWM_STEP);
    applyMotorOutput(activeDirection, currentPwm);
  }
}

// --- Periodic position reporting ---
void reportPosition() {
  unsigned long now = millis();
  if (now - lastPosReportMs < POS_REPORT_INTERVAL_MS) {
    return;
  }
  lastPosReportMs = now;

  Serial.print("POS ");
  Serial.println(encoderCount);
}

// --- LED breathing animation ---
unsigned long lastLedUpdateMs = 0;
const unsigned long LED_UPDATE_INTERVAL_MS = 30;  // ~33fps, limits interrupt-off time from strip.show()

void updateLEDs() {
  if (!breatheEnabled) return;
  if (baseR == 0 && baseG == 0 && baseB == 0) return;

  // Don't update LEDs while motor is active — strip.show() disables interrupts
  // and causes encoder ticks to be missed
  if (gotoActive || activeDirection != DIR_STOPPED) return;

  unsigned long now = millis();
  if (now - lastLedUpdateMs < LED_UPDATE_INTERVAL_MS) return;
  lastLedUpdateMs = now;

  // Sine-wave breathing: phase relative to last color change so it starts at peak
  float phase = (float)((now - breatheStartMs) % BREATHE_PERIOD_MS) / BREATHE_PERIOD_MS;
  // cos gives smooth 1→-1→1 over one cycle; remap to BREATHE_MIN..BREATHE_MAX
  float wave = (cos(phase * 2.0 * PI) + 1.0) / 2.0;  // 0.0 .. 1.0
  float scale = ((float)BREATHE_MIN + wave * (float)(BREATHE_MAX - BREATHE_MIN)) / 255.0;

  uint8_t r = (uint8_t)(baseR * scale);
  uint8_t g = (uint8_t)(baseG * scale);
  uint8_t b = (uint8_t)(baseB * scale);

  strip.fill(strip.Color(r, g, b));
  strip.show();
}

// --- Setup & Loop ---
void setup() {
  pinMode(RPWM, OUTPUT);
  pinMode(LPWM, OUTPUT);
  pinMode(REN, OUTPUT);
  pinMode(LEN, OUTPUT);

  digitalWrite(REN, HIGH);
  digitalWrite(LEN, HIGH);

  stopMotor();

  // Encoder setup
  pinMode(ENCODER_A_PIN, INPUT_PULLUP);
  pinMode(ENCODER_B_PIN, INPUT_PULLUP);
  attachInterrupt(digitalPinToInterrupt(ENCODER_A_PIN), encoderISR_A, CHANGE);
  attachInterrupt(digitalPinToInterrupt(ENCODER_B_PIN), encoderISR_B, CHANGE);

  Serial.begin(BAUD_RATE);

  // NeoPixel setup
  strip.begin();
  strip.setBrightness(40);  // cap brightness to limit current (~400mA safe for UNO 5V rail)
  strip.show();

  Serial.println("READY");
}

void loop() {
  readSerialCommands();
  updateGoto();
  updateMotion();
  reportPosition();
  updateLEDs();
}
