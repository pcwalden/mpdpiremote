import time
import logging
import threading
from threading import Lock, Thread, Barrier
import pifacecad
from pifacecad.lcd import LCD_WIDTH, LCD_MAX_LINES, LCD_RAM_WIDTH

LCD_LINE_WIDTH = int(LCD_RAM_WIDTH / LCD_MAX_LINES)

logger = logging.getLogger('marquee')

class Marquee:
    def __init__(self, pifacecad_lcd):
        self._dlock = Lock() # internal lock for the lcd display
        self.display = pifacecad_lcd
        self.marquee_cnt = 0
        self.marquee_timer = None
        self.backlight_timers = []
        self.backlight_duration = 30.0 # seconds
        self.marquee_initial_shift_delay = 2.0 # seconds
        self.marquee_shift_delay = 1.25 # seconds
    
    def backlightoff(self):
        logger.debug('Calling backlightoff()')
        self.backlight_timers.pop(0)
        logger.debug('backlight pop')
        if not self.backlight_timers:
            self._dlock.acquire()
            self.display.backlight_off()
            self._dlock.release()
            logger.debug('backlight off')
    
    def cancel_timers(self):
        logger.debug('Calling cancel_timers()')
        if self.marquee_timer:
##            print('canceling marquee timer')
            self.marquee_timer.cancel()
        while self.backlight_timers:
            logger.debug('canceling backlight timers')
            self.backlight_timers.pop(0).cancel()
    
    def backlight_timer(self):
        logger.debug('Calling backlight_timer()')
        if self.backlight_duration > 0.1:
            if len(self.backlight_timers) <= 0:
                self._dlock.acquire()
                self.display.backlight_on()
                self._dlock.release()
                logger.debug('backlight on')
            self.backlight_timers.append(threading.Timer(self.backlight_duration,
                                                         self.backlightoff))
            self.backlight_timers[-1].start()
            logger.debug('backlight push')
        elif self.backlight_duration < -0.9:
            self._dlock.acquire()
            self.display.backlight_on()
            self._dlock.release()
    
    def marquee_shift(self): 
        logger.debug('Calling marquee_shift()')
        self.marquee_timer = None
        self.marquee_cnt -= 1
        self._dlock.acquire()
        self.display.move_left()
        self._dlock.release()
        if self.marquee_cnt > 0:
            self.marquee_timer = threading.Timer(self.marquee_shift_delay,
                                                 self.marquee_shift)
            self.marquee_timer.start()
    
    def _marqlen(self, items):
        itemslen = 0
        truncateditems = []
        for item in items:
            if itemslen < LCD_LINE_WIDTH:
                if type(item) is int:
                    itemslen += 1
                elif type(item) is str:
                    if itemslen + len(item) > LCD_LINE_WIDTH:
                        item = item[0:LCD_LINE_WIDTH-itemslen-1] #truncate item
                    itemslen += len(item)
                truncateditems.append(item)
            else:
                break # no more room on the LCD line
        return itemslen, truncateditems
    
    def _marq_write(self, items):
##        Assumes _dlock is acquired before calling
        logger.debug('Calling _marq_write('+str(items)+')')
        for item in items:
            if type(item) is int:
                self.display.write_custom_bitmap(item)
            elif type(item) is str:
                self.display.write(item)
    
    def marquee_start(self, text, text2=None):
        logger.debug('Calling marquee_start('+str(text)+','+str(text2)+')')
        if self.marquee_timer:   # cancel any marquee underway
            self.marquee_timer.cancel()
            self.marquee_timer = None
        if type(text) is str:
            text = [text]
        if type(text2) is str:
            text2 = [text2]
        
        (dsp_len, text) = self._marqlen(text) # trim line1 to display width
        dsp_len2 = 0
        if text2:
            (dsp_len2,text2) = self._marqlen(text2)  # trim line2 to display width
        self.marquee_cnt = max(max(dsp_len - LCD_WIDTH, 0),
                               max(dsp_len2 - LCD_WIDTH, 0))
        self.backlight_timer()
        self._dlock.acquire()
        self.display.clear()
        self._marq_write(text)
        if text2:
            self.display.write("\n")
            self._marq_write(text2)
        self._dlock.release()
        if self.marquee_cnt > 0:
            #print('starting marquee for ' + str(self.marquee_cnt) + ' shifts')
            self.marquee_timer = threading.Timer(self.marquee_initial_shift_delay,
                                                 self.marquee_shift)
            self.marquee_timer.start()
        #else:
            #print('no marquee started')
    
    def marquee(self, lines):
        logger.debug('Calling marquee('+str(lines)+')')
        self.marquee_start(lines[0], lines[1])

class LockableMarquee(Marquee):
    def __init__(self, pifacecad_lcd):
        super(LockableMarquee, self).__init__(pifacecad_lcd)
        self._lock = Lock()
    def acquire(self,blocking=True, timeout=-1):
        #print('acquiring LCD lock')
        return self._lock.acquire(blocking, timeout)
        #print('acquired LCD lock')
    def release(self):
        self._lock.release()
        #print('released LCD lock')
    def __enter__(self):
        self.acquire()
    def __exit__(self, type, value, traceback):
        self.release()

if __name__ == '__main__':
    testlogger = logging.getLogger()
    testlogger.setLevel('DEBUG')
    testlogger.info('starting LockableMarquee test')
    CAD = pifacecad.PiFaceCAD()
    DISPLAY = CAD.lcd
    updown = pifacecad.LCDBitmap([0x4,0xe,0x1f,0x0,0x0,0x1f,0xe,0x4])
    CAD.lcd.store_custom_bitmap(0, updown)
    updown = 0
    LM = LockableMarquee(DISPLAY)
    with LM:
        LM.backlight_timer()
        LM.marquee_start('1', 'one456789012345678901234567890')
        time.sleep(5)
        LM.backlight_timer()
        LM.marquee_start(['2', updown, '+', updown], 'two456789012345678901234567890')
        time.sleep(5)
        LM.backlight_timer()
        LM.marquee_start('3', 'three')
        time.sleep(3)
        LM.backlight_timer()
        LM.marquee_start('4', 'four')
        time.sleep(3)
        LM.backlight_timer()
        LM.marquee_start('5', 'five')
    with LM:
        time.sleep(5)
        LM.cancel_timers()
        LM.marquee_start('bye', 'bye')
        LM.cancel_timers()
