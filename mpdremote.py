#! /usr/bin/python3
import sys
import os
import time
import threading
import argparse
import logging
import configparser
from time import localtime, strftime
from threading import Lock, Thread, Barrier
import pifacecad
from mpd import (MPDClient, CommandError, ConnectionError, PendingCommandError)
from socket import error as SocketError
from socket import timeout as SocketTimeout
import pydaemon
from fsm import (Fsm, State)
from weather import WeatherStation
from pifacemarquee import LockableMarquee
from mpdpreferences import MpdPreferences
from select import select

config = configparser.ConfigParser()
config.read_dict({'preferences': {'volumeincrement': '10',
                                  'backlight_duration': '30.0',
                                  'info_interval': '60',
                                  'display_info': 'Time',
                                  'snooze_interval': '30',
                                  },
                  })
config.read(['/etc/mpdremoterc', # add to install when finished development
             './mpdremoterc',    # delete this when finished development
             os.path.expanduser('~/.mpdremoterc'), # personal static prefs here
             os.path.expanduser('~/.mpdremoteprefs'), #dyn prefs recorded here
             ])

class PreferenceMenu:
    def __init__(self, configparser):
        self.config = configparser
        self.menus = []
        self.nmenus = len(self.menus)
        self.currmenu = 0
        self.label = None
        self.choices = []
        self.currchoice = 0
        self.currvalue = None
        self.nchoices = 0
        self.refresh()
    
    def refresh(self):
        menulabels = eval(self.config['preferencemenu'].get('menu_choices','()'))
        self.menus = []
        for menulabel in menulabels:
            menu = menulabel.split()[0]
            menuchoices = eval(self.config['preferencemenu'].get(menu+'_choices'))
            menuvalue = self.config['preferencemenu'].get(menu+'_value')
            menuonchange = self.config['preferencemenu'].get(menu+'_on_value_change',
                                                             'pass')
            self.menus.append({'label':menulabel,
                               'choices':menuchoices,
                               'value':menuvalue,
                               'on_change':menuonchange,
                               })
        self.nmenus = len(self.menus)
        self.currmenu = 0
        self.label = None
        self.choices = []
        self.currchoice = 0
        self.nchoices = 0
##        print(self.menus)
        return self
    
    def save(self):
        pass

    def show(self):
        try:
            if self.nmenus > 0:
                menu = self.menus[self.currmenu]
                label = menu['label']
                value = self.config['preferences'][menu['value']]
                return [label, [left,updown,right,' ',value]]
        except KeyError as ke:
            logging.error('"'+str(ke)+'" not found in preferences')
        return [label, [left,updown,right,' ','not set']]
    
    def up(self):
        self.currmenu = (self.currmenu - 1) % len(self.menus)
        return self.show()
    
    def down(self):
        self.currmenu = (self.currmenu + 1) % len(self.menus)
        return self.show()

    def startchoice(self):
        menu = self.menus[self.currmenu]
##        print(menu)
        self.label = menu['label']
        self.choices = menu['choices']
        self.nchoices = len(self.choices)
        self.currvalue = self.config['preferences'].get(menu['value'], 'not set')
##        print(value)
        try:
            self.currchoice = self.choices.index(self.currvalue)
        except ValueError:
            self.currchoice = 0
    
    def showchoice(self):
        value = self.choices[self.currchoice]
        return [self.label, [left,updown,ok,'-set to: ',value]]
    
    def setchoice(self):
        menu = self.menus[self.currmenu]
        value = self.choices[self.currchoice]
        if self.currvalue != value:
            self.config.set('preferences', menu['value'], value)
            logging.info('set preferences: '+menu['value']+' = '+value)
            self.savechoices()
            try:
                if menu['on_change'] != 'pass':
                    logging.debug('on_change: eval('+menu['on_change']+')')
                    eval(menu['on_change'])
                    logging.debug('on_change: exit eval('+menu['on_change']+')')
            except NameError:
                logging.warning(menu['label']+' changed, but '+menu['on_change']+' not found')
        else:
            logging.debug('No change in preference')
    
    def savechoices(self):
        preferences = self.config['preferences']
        try:
            fd = open(os.path.expanduser('~/.mpdremoteprefs'),'w')
            fd.write("[preferences]\n")
            for key in preferences:
                value = preferences[key].replace('%', '%%') # escape % characters
                fd.write('    '+key+" = "+value+"\n")
            fd.close()
        except IOError as err:
            logging.warning('cannot save preference change to .mpdremoteprefs: '+ str(err))
    
    def upchoice(self):
        self.currchoice = (self.currchoice - 1) % len(self.choices)
        return self.show()
    
    def downchoice(self):
        self.currchoice = (self.currchoice + 1) % len(self.choices)
        return self.show()

class LockableMPDClient(MPDClient):
    def __init__(self, use_unicode=False):
        super(LockableMPDClient, self).__init__()
        self.use_unicode = use_unicode
        self._lock = Lock()
    def acquire(self):
        self._lock.acquire()
    def release(self):
        self._lock.release()
    def __enter__(self):
        self.acquire()
    def __exit__(self, type, value, traceback):
        self.release()

class MPDSongEntry:
    def __init__(self, mpd_song_dictionary):
        self.entry = mpd_song_dictionary

    def title(self):        
        if 'title' in self.entry:
            title = self.entry['title']
        elif 'name' in self.entry:
            title = self.entry['name']
        else:
            title = 'unknown'
        return title
    
    def album(self):
        try:
            album = self.entry['album']
        except (AttributeError, KeyError):
            album = ''
        return album
    
    def artist(self):
        try:
            artist = self.entry['artist']
        except (AttributeError, KeyError):
            artist = ''
        return artist
    
    def title_album(self):
        return [self.title(), self.album()]
    
    def title_artist(self):
        return [self.title(), self.artist()]

class MPDStatus:
    def __init__(self, mpd_client):
        self.mpd_client = mpd_client
        self.status = {}
    
    def refresh(self):
        try:
            self.status = self.mpd_client.status()
        except (ConnectionError, SocketError, SocketTimeout, IOError):
            LM.marquee_start('not connected')
            self.status = {}
        return self
    
    def random(self, toggle = False):
        if len(self.refresh().status) > 0:
            if toggle:
                self.status['random'] = '0' if self.status['random'] == '1' else '1'
                self.mpd_client.random(self.status['random'])
            return 'On' if self.status['random'] == '1' else 'Off'
        else:
            return ''
    
    def consume(self, toggle = False):
        if len(self.refresh().status) > 0:
            if toggle:
                self.status['consume'] = '0' if self.status['consume'] == '1' else '1'
                self.mpd_client.consume(self.status['consume'])
            return 'On' if self.status['consume'] == '1' else 'Off'
        else:
            return ''
    
    def repeat(self, toggle = False):
        if len(self.refresh().status) > 0:
            if toggle:
                self.status['repeat'] = '0' if self.status['repeat'] == '1' else '1'
                self.mpd_client.repeat(self.status['repeat'])
            return 'On' if self.status['repeat'] == '1' else 'Off'
        else:
            return ''
    
    def volume (self, event=None):
        if len(self.refresh().status) > 0:
            try:
                volume = int(self.status['volume'])
                volumeincr = config['preferences'].getint(
                    'volumeincrement')
                if event:
                    if event == 'volumeup':
                        volume += volumeincr
                    elif event == 'volumedown':
                        volume -= volumeincr
                    volume = max(volume, 0)
                    volume = min(volume,100)
                    self.mpd_client.setvol(str(volume))
                return 'volume: ' + str(volume) + '%'
            except (ConnectionError, SocketError, SocketTimeout, IOError):
                return 'not connected'
        else:
            return ''
    
    def time(self, event=None):
        try:
            if event:
                pos = int(self.refresh().status['time'].split(':')[0])
                song = self.status['song']
                if event == 'advance':
                    pos += 10
                elif event == 'replay':
                    pos -= 10
                self.mpd_client.seek(song, str(pos))
            else:
                return self.refresh().status['time']
        except (ConnectionError, SocketError, SocketTimeout, IOError):
            return 'not connected'
        except (AttributeError, KeyError):
            return ''
    
    def single(self, toggle = False):
        if len(self.refresh().status) > 0:
            if toggle:
                self.status['single'] = '0' if self.status['single'] == '1' else '1'
                self.mpd_client.single(self.status['single'])
            return 'On' if self.status['single'] == '1' else 'Off'
        else:
            return ''
    
    def playqueuestats(self, toggle = False):
        self.refresh()
        if 'song' not in self.status or 'playlistlength' not in self.status or int(self.status['playlistlength']) <= 0:
            return [0,0]
        return [int(self.status['song']), int(self.status['playlistlength'])]

class MPDPlaylist:
    def __init__(self, mpd_client, playlist_name=None):
        self.mpd_client = mpd_client
        self.index = 0
        self.listlen = 0
        self.playlistnm = None
        self.playlist = None
        if playlist_name:
            self.fetch(playlist_name)
    
    def fetch(self, playlist_name):
        try:
            self.playlistnm = playlist_name
            self.playlist = self.mpd_client.listplaylistinfo(playlist_name)
            self.listlen = len(self.playlist)
            self.index = 0
        except (CommandError, ConnectionError, SocketError, SocketTimeout, IOError):
            self.index = 0
            self.listlen = 0
            self.playlistnm = None
            self.playlist = None
    
    def title(self):
        if self.playlist:
            song = MPDSongEntry(self.playlist[self.index])
            return song.title()
        else:
            return 'no playlist'
    
    def uptitle(self):
        if self.playlist:
            self.index = self.index - 1 if self.index > 0 else self.listlen -1
            return self.title()
        else:
            return 'no playlist'
    
    def downtitle(self):
        if self.playlist:
            self.index = self.index + 1 if self.index < self.listlen-1 else 0
            return self.title()
        else:
            return 'no playlist'
    
    def select(self, playnow=False):
        if self.playlist:
            logging.info('Adding '+self.playlist[self.index]['file']+' to playqueue')
            addedid = self.mpd_client.addid(self.playlist[self.index]['file'])
            if playnow: self.mpd_client.playid(addedid)

class MPDCurrentPlaylist:
    def __init__(self, mpd_client):
        self.mpd_client = mpd_client
        self.index = None
        self.currplslen = 0

    def updatelist(self):
        try:
            status = self.mpd_client.status()
            if 'playlistlength' not in status:
                self.index = None
                self.currplslen = 0
                logging.debug('MPDCurrentPlaylist.updatelist: no play queue')
                logging.debug(str(status))
                return self
            elif 'song' in status:
                self.index = int(status['song'])
                logging.debug('MPDCurrentPlaylist.updatelist: song idx reset to '+str(self.index))
            else:
                self.index = 0
            self.currplslen = int(status['playlistlength'])
        except (ConnectionError, SocketError, SocketTimeout, IOError):
            self.index = None
            self.currplslen = 0
        return self
    
    def refresh(self):
        try:
            status = self.mpd_client.status()
            if 'playlistlength' not in status:
                self.index = None
                self.currplslen = 0
                logging.debug('MPDCurrentPlaylist.refresh: no play queue')
                logging.debug(str(status))
            else:
                self.currplslen = int(status['playlistlength'])
        except (ConnectionError, SocketError, SocketTimeout, IOError):
            self.index = None
            self.currplslen = 0
        return self
    
    def songentry(self, index=None):
        self.refresh()
        if self.currplslen <= 0:
            logging.debug('songentry: no play queue')
            return MPDSongEntry({'title': 'no play queue', 'album': ''})
        plsmaxidx = self.currplslen - 1
        if index:
            self.index = index
            logging.debug('MPDCurrentPlaylist song idx set to '+str(self.index))
        self.index = min(max(0, self.index),plsmaxidx)
        return MPDSongEntry(self.mpd_client.playlistid()[self.index])
    
    def song(self):
        return self.songentry().title()

    def upsong(self):
        self.refresh()
        if self.currplslen <= 0:
            return 'no play queue'
        plsmaxidx = self.currplslen - 1
        self.index = min(max(0, self.index - 1),plsmaxidx)
        logging.debug('MPDCurrentPlaylist song idx set to '+str(self.index))

    def downsong(self):
        self.refresh()
        if self.currplslen <= 0:
            return 'no play queue'
        plsmaxidx = self.currplslen - 1
        self.index = min(max(0, self.index + 1),plsmaxidx)
        logging.debug('MPDCurrentPlaylist song idx set to '+str(self.index))

    def selectsong(self):
        self.refresh()
        if self.currplslen <= 0:
            return 'no play queue'
        plsmaxidx = self.currplslen - 1
        self.index = min(max(0, self.index),plsmaxidx)
        self.mpd_client.play(str(self.index))
    
    def deletesong(self):
        self.refresh()
        if self.currplslen <= 0:
            return 'no play queue'
        plsmaxidx = self.currplslen - 1
        self.index = min(max(0, self.index),plsmaxidx)
        self.mpd_client.delete(str(self.index))

class MPDPlaylists:
    def __init__(self, mpd_client):
        self.mpd_client = mpd_client
        self.playlists = []
        self.plsmaxidx = 0
        self.index = None

    def refresh(self):
        try:
            self.playlists = sorted(self.mpd_client.listplaylists(),
                                    key=lambda item: item['playlist'].lower())
            self.plsmaxidx = len(self.playlists)-1
            self.index = 0
        except (ConnectionError, SocketError, SocketTimeout, IOError):
            self.playlists = []
            self.plsmaxidx = 0
            self.index = None
        return self

    def pls(self):
        if len(self.playlists) > 0:
            return self.playlists[self.index]['playlist']
        else:
            return 'no playlists'

    def uppls(self):
        if len(self.playlists) > 0:
            self.index = self.index - 1 if self.index > 0 else self.plsmaxidx
            return self.playlists[self.index]['playlist']
        else:
            return 'no playlists'
   
    def downpls(self):
        if len(self.playlists) > 0:
            self.index = self.index + 1 if self.index < self.plsmaxidx else 0
            return self.playlists[self.index]['playlist']
        else:
            return 'no playlists'
    
    def selectpls(self):
        if len(self.playlists) > 0:
            logging.info(
                'Replace playlist: '+self.playlists[self.index]['playlist'])
            self.mpd_client.clear()
            self.mpd_client.load(self.playlists[self.index]['playlist'])
            self.mpd_client.play()
            self.playlists = []
            self.plsmaxidx = 0
            self.index = 0
    
    def addpls(self):
        if len(self.playlists) > 0:
            logging.info(
                'Add playlist: '+self.playlists[self.index]['playlist'])
            (curr_song, queuelen) = MPDStatus(self.mpd_client).playqueuestats()
            self.mpd_client.load(self.playlists[self.index]['playlist'])
            self.mpd_client.play(str(queuelen))
            self.playlists = []
            self.plsmaxidx = 0
            self.index = 0        

class MPDdatabase:
    def __init__(self, mpd_client):
        self.mpd_client = mpd_client
        self.index = []
        self.listlen = 0
        self.path = []
        self.dirlist = []
    
    def refresh(self):
        try:
            self.dirlist.append(self.mpd_client.lsinfo())
##          Remove any playlist entries from the database lsinfo() call
            for idx in range(len(self.dirlist[-1])):
                if 'playlist' in self.dirlist[-1][idx]:
                    break
            del self.dirlist[-1][idx:len(self.dirlist[-1])]
            self.listlen = len(self.dirlist[-1])
            self.index.append(0)
            self.path.append('')
        except (ConnectionError, SocketError, SocketTimeout, IOError):
            self.index = []
            self.listlen = 0
            self.path = []
            self.dirlist = []
    
    def enter(self):
        try:
            entry = self.dirlist[-1][self.index[-1]]
            if 'directory' in entry:
                self.path.append(entry['directory'])
                logging.debug('entering ' + self.path[-1])
                self.dirlist.append(
                    self.mpd_client.lsinfo(self.path[-1]))
                self.index.append(0)
                self.listlen = len(self.dirlist[-1])
            else:
                pass
        except (KeyError, IndexError, ConnectionError, SocketError, SocketTimeout, IOError):
            pass
    
    def back(self):
        try:
            if len(self.dirlist) > 1:
                self.dirlist.pop()
                self.index.pop()
                self.path.pop()
            self.listlen = len(self.dirlist[-1])
        except (KeyError, IndexError, ConnectionError, SocketError, SocketTimeout, IOError):
            pass
    
    def entry(self):
        try:
            return self.dirlist[-1][self.index[-1]]
        except (KeyError, IndexError, ConnectionError, SocketError, SocketTimeout, IOError):
            return {}
    
    def upentry(self):
        try:
            self.index[-1] = self.index[-1] - 1 if self.index[-1] > 0 else self.listlen-1
        except (KeyError, IndexError, ConnectionError, SocketError, SocketTimeout, IOError):
            pass
    
    def downentry(self):
        try:
            self.index[-1] = self.index[-1] + 1 if self.index[-1] < self.listlen-1 else 0
        except (KeyError, IndexError, ConnectionError, SocketError, SocketTimeout, IOError):
            pass
    
    def select(self, playnow=False):
        try:
            entry = self.dirlist[-1][self.index[-1]]
            if 'directory' in entry:
                pos = MPDStatus(self.mpd_client).playqueuestats()[1]
                logging.info('Adding '+entry['directory']+' to playqueue')
                self.mpd_client.add(entry['directory'])
                if playnow: self.mpd_client.play(pos)
            elif 'file' in entry:
                addedid = self.mpd_client.addid(entry['file'])
                logging.info('Adding '+entry['file']+' to playqueue')
                if playnow: self.mpd_client.playid(addedid)
        except (KeyError, IndexError, ConnectionError, SocketError, SocketTimeout, IOError):
            pass

MENUS = PreferenceMenu(config)
MPD = LockableMPDClient() # for sending commands and getting status
MPD2 = LockableMPDClient() # for idle updates only
mpdcurrplaylist = MPDCurrentPlaylist(MPD)
mpdplaylists = MPDPlaylists(MPD)
mpdplaylist = MPDPlaylist(MPD)
mpdstatus = MPDStatus(MPD)
CAD = pifacecad.PiFaceCAD()
updown = pifacecad.LCDBitmap([0x4,0xe,0x1f,0x0,0x0,0x1f,0xe,0x4])
CAD.lcd.store_custom_bitmap(0, updown)
updown = 0
right = pifacecad.LCDBitmap([0x0,0x8,0xc,0xe,0xc,0x8,0x0,0x0])
CAD.lcd.store_custom_bitmap(1, right)
right = 1
left = pifacecad.LCDBitmap([0x0,0x2,0x6,0xe,0x6,0x2,0x0,0x0])
CAD.lcd.store_custom_bitmap(2, left)
left = 2
ok = pifacecad.LCDBitmap([0xc,0x12,0x12,0xc,0x0,0x5,0x6,0x5])
CAD.lcd.store_custom_bitmap(3, ok)
ok = 3
retrn = pifacecad.LCDBitmap([0x0,0x0,0x1,0x12,0x14,0x18,0x1e,0x0])
CAD.lcd.store_custom_bitmap(4, retrn)
retrn = 4
degF = pifacecad.LCDBitmap([0x1c,0x14,0x1c,0x7,0x4,0x6,0x4,0x4])
CAD.lcd.store_custom_bitmap(5, degF)
degF = 5
mph1 = pifacecad.LCDBitmap([0x1a,0x15,0x15,0x0,0x1,0x2,0x0,0x0])
CAD.lcd.store_custom_bitmap(6, mph1)
mph1 = 6
mph2 = pifacecad.LCDBitmap([0x0,0x4,0x8,0x14,0x4,0x6,0x5,0x5])
CAD.lcd.store_custom_bitmap(7, mph2)
mph2 = 7
LM = LockableMarquee(CAD.lcd)
LM.backlight_duration = config['preferences'].getfloat('backlight_duration')
stop_now = False
pinger = None
idlethread = None
snoozetimer = None
stationlist = eval(config['staticpreferences'].get('weather_stations','()'))
stations = []
for entry in stationlist:
    stations.append(WeatherStation(entry['location'], entry['id']))

class MPDdatabaseMenu(MPDdatabase):
    def __init__(self, mpd_client):
        super(MPDdatabaseMenu, self).__init__(mpd_client)
    
    def entry(self):
        entry = super(MPDdatabaseMenu, self).entry()
        menu = [retrn,left,updown] if len(self.dirlist) > 1 else [retrn,updown]
        if 'title' in entry or 'file' in entry:
            menu.extend([ok,'-add,2-play'])
            return [MPDSongEntry(entry).title(), menu]
        elif 'directory' in entry:
            menu.extend([right,ok,'-add,2-play'])
            return [entry['directory'].split('/')[-1], menu]
        else:
            return ['no connection',[retrn]]

mpddatabase = MPDdatabaseMenu(MPD)

def disconnect_clients():
    try:
        logging.debug('disconnect MPD')
        MPD.disconnect()
    except ConnectionError:
        pass
    try:
        MPD2.noidle()
    except (CommandError, ConnectionError):
        pass
    try:
        logging.debug('disconnect MPD2')
        MPD2.disconnect()
    except (CommandError, ConnectionError):
        pass
    logging.debug('exit disconnect_clients()')

def connect_client(mpc, label='MPD'):
    mpdrec = MpdPreferences().preferredClient(config)
    logging.info(label+' connecting to '+mpdrec['name'])
    if mpdrec['host']:
        try:
            mpc.connect(mpdrec['host'],
                        mpdrec['port'])
            logging.info(label+' connected to '+mpdrec['name'])
            if LM.acquire(False):
                logging.debug('have display lock, showing info')
                LM.marquee_start(label+' connected')
                logging.debug('releasing display lock')
                LM.release()
        except (ConnectionError, SocketError, SocketTimeout, IOError) as ex:
            if str(ex) == "already connected":
                pass
            else:
                logging.warning(label+' connect failed: '+str(ex))
                if LM.acquire(False):
                    logging.debug('have display lock, showing info')
                    LM.marquee_start(label+' connect failed', str(ex))
                    logging.debug('releasing display lock')
                    LM.release()
    else: # the mpd server had gone off-line
        logging.warning(label+' server '+mpdrec['name']+' is no longer available')

def connect_clients():
    connect_client(MPD, 'MPD')
    connect_client(MPD2, 'MPD2')

def reconnect_clients():
    disconnect_clients()
    connect_clients()

def cancel_timers():
    global infoloop
    global snoozetimer
    if pinger:
        logging.info('canceling pinger')
        pinger.cancel()
    if infoloop:
        logging.info('canceling infolooper')
        infoloop.cancel()
    if snoozetimer:
        logging.info('canceling snooze timer')
        snoozetimer.cancel()
    LM.cancel_timers()
    logging.info('canceled marquee timers')

def power_off(event):
    global stop_now
    global end_barrier
##    print(event.ir_code)
    stop_now = True
    cancel_timers()
    try:
        logging.info('canceling idleloop')
        MPD2.noidle()
    except (CommandError, ConnectionError, SocketError, SocketTimeout, IOError):
        pass
    end_barrier.wait()

def ping():
    global stop_now
    global pinger
    if not stop_now:
        with MPD:
            try:
                MPD.ping()
            except SocketTimeout as to:
                logging.warning('MPD timeout, disconnecting')
                logging.info(
                    'MPD will try again in '+
                    config['staticpreferences'].get('ping_interval','59.0')+
                    ' secs')
                MPD.disconnect()
                with LM:
                    LM.marquee_start('MPD reconnecting', str(to))
            except (ConnectionError, SocketError, IOError):
                logging.info('Need to establish connection for MPD')
                mpdrec = MpdPreferences().preferredClient(config)
                if mpdrec['host']:
                    try:
                        MPD.connect(mpdrec['host'],
                                    mpdrec['port'])
                        logging.info('MPD connected to '+mpdrec['name'])
                        with LM:
                            LM.marquee_start('MPD connected')
                    except ConnectionError as err:
                        if str(err) == "already connected":
                            MPD.disconnect()
                        logging.warning('MPD connect failed: '+str(err))
                        logging.info(
                            'MPD will try again in '+
                            config['staticpreferences'].get('ping_interval','59.0')+
                            ' secs')
                        with LM:
                            LM.marquee_start('MPD connect failed', str(ex))
                    except (SocketError, SocketTimeout, IOError) as ex:
                        logging.warning('MPD connect failed: '+str(ex))
                        logging.info(
                            'MPD will try again in '+
                            config['staticpreferences'].get('ping_interval','59.0')+
                            ' secs')
                        with LM:
                            LM.marquee_start('MPD connect failed', str(ex))
                else: # the mpd server had gone off-line
                    logging.warning('MPD server '+mpdrec['name']+' is no longer available')
                    logging.info(
                        'MPD will try again in '+
                        config['staticpreferences'].get('ping_interval','59.0')+
                        ' secs')
        pinger = threading.Timer(float(config['staticpreferences'].
                                 get('ping_interval','59.0')),
                                 ping)
        pinger.start()
    else:
        logging.debug('stopping MPD pinger')
        pinger = None

infoloopcount = 0
def infolooper():
    global stop_now
    global infoloop
    global infoloopcount
    infoloopcount += 1
    logging.debug('infoloopcount = '+str(infoloopcount))
    if not stop_now:
        if LM.acquire(False):
            logging.debug('have display lock, showing info')
            display_type = config['preferences'].get('display_info')
            if display_type == 'Time' or (display_type == 'Alternate' and infoloopcount % 2):
                logging.debug('showing time')
                LM.marquee_start(strftime(config['staticpreferences'].
                                          get('ping_timeformat1',"%I:%M %p"),
                                          localtime()),
                                 strftime(config['staticpreferences'].
                                          get('ping_timeformat2',"%a %b %d %Y"),
                                          localtime()))
            elif display_type == 'Weather' or (display_type == 'Alternate' and (infoloopcount+1) % 2):
                loopcount = int(infoloopcount/2) if display_type == 'Alternate' else infoloopcount
                stn_idx = loopcount % len(stations)
                logging.debug('showing weather station: '+str(stn_idx))
                station = stations[stn_idx]
                try:
                    station.generate_xmltree()
                    LM.marquee_start(station.location,
                                     [station.temperaturef,degF,' ',
                                      station.wind_dir,
                                      station.wind_mph,mph1,mph2])
                except Exception as err:
                    logging.error(station.location+': '+station.weather_id+': '+str(err))
            logging.debug('releasing display lock')
            LM.release()
        else:
            logging.debug('cannot get display lock; skipping info')
        infoloop = threading.Timer(float(config['preferences'].
                                 getfloat('info_interval')),
                                 infolooper)
        infoloop.start()
    else:
        logging.debug('stopping infolooper')
        infoloop = None

def idleloop():
    global stop_now
    firstpass = True
    event = {}
    if not stop_now:
        logging.debug('begin MPD2 idleloop')
        while not stop_now:
            try:
                with MPD2:
                    if firstpass:
                        with LM:
                            MPD2.ping()
                            LM.marquee(MPDCurrentPlaylist(MPD2).
                                             updatelist().
                                             songentry().
                                             title_album())
                        firstpass = False
##                    event = MPD2.idle()
                    MPD2.send_idle()
                    # wait for event with 5 minute timeout
                    canRead = select([MPD2], [], [], 300.0)[0]
                    logging.debug(str(canRead))
                    if canRead:
                        logging.debug('retrieving idle event')
                        event = MPD2.fetch_idle()
                    else: # timeout, have to reset idle logic
                        try:
                            logging.debug('idle timeout: send noidle')
                            MPD2.noidle()
                        except (CommandError, ConnectionError):
                            logging.debug('exception on noidle')
                            pass
                if 'playlist' in event:
                    logging.debug('process '+str(event))
                    with MPD2, LM:
                        LM.marquee(MPDCurrentPlaylist(MPD2).
                                   updatelist().
                                   songentry().
                                   title_album())
                        logging.debug('end processing playlist')
                elif 'mixer' in event:
                    logging.debug('process '+str(event))
                    with MPD2, LM:
                        volumestr = MPDStatus(MPD2).volume()
                        LM.marquee_start(MPDCurrentPlaylist(MPD2).
                                         updatelist().song(),
                                         volumestr)
                        logging.debug('end processing mixer')
                elif 'player' in event:
                    logging.debug('process '+str(event))
                    with MPD2, LM:
                        status = MPDStatus(MPD2)
                        timestat = status.time()
                        state = status.status['state']
                        LM.marquee_start(MPDCurrentPlaylist(MPD2).
                                         updatelist().song(),
                                         state+' at '+timestat)
                        logging.debug('end processing player')
                else:
                    logging.debug('ignored event: '+str(event))
            except (PendingCommandError, SocketTimeout, SocketError) as to:
                if not stop_now:
                    logging.warning(str(to)+': MPD2 problem, disconnecting: '+str(to))
                with MPD2, LM:
                    try:
                        MPD2.disconnect()
                    except ConnectionError: # if already disconnect, ignore exception
                        pass
                    if not stop_now:
                        LM.marquee_start('MPD2 reconnecting', str(to))
            except (ConnectionError, IOError) as exp:
                logging.info(str(exp)+': Need to establish connection for MPD2')
                mpdrec = MpdPreferences().preferredClient(config)
                if mpdrec['host']:
                    try:
                        with MPD2:
                            MPD2.connect(mpdrec['host'],
                                         mpdrec['port'])
                        logging.info('MPD2 connected to '+mpdrec['name'])
                    except ConnectionError as err:
                        if str(err) == "already connected":
                            MPD2.disconnect()
                        logging.warning('MPD2 connect failed: '+str(err))
                        logging.info('MPD2 will try again')
                        with LM:
                            LM.marquee_start('MPD2 connect failed', str(err))
                    except (SocketError, SocketTimeout, IOError) as ex:
                        logging.warning('MPD2 connect failed: '+str(ex))
                        logging.info('MPD2 will try again in 60 secs')
                        with LM:
                            LM.marquee_start('MPD2 connect failed', str(ex))
                        time.sleep(60)
                else: # the mpd server had gone off-line
                    logging.warning('MPD2 server '+mpdrec['name']+' is no longer available')
                    logging.info('MPD2 will try again in 60 secs')
                    time.sleep(60)
            event = {}
            logging.debug('MPD2 idle loop bottom')
        logging.info('MPD2 idleloop terminating')
    else:
        logging.info('stopping MPD2 idleloop')

def play(event):
    with MPD:
        try:
            MPD.play() # player change display handled by idleloop
        except (ConnectionError, SocketError, SocketTimeout, IOError):
            with LM:
                LM.marquee_start('not connected')

def pause(event):
    with MPD:
        try:
            MPD.pause() # player change display handled by idleloop
        except (ConnectionError, SocketError, SocketTimeout, IOError):
            with LM:
                LM.marquee_start('not connected')

def stop(event):
    with MPD:
        try:
            MPD.stop() # player change display handled by idleloop
        except (ConnectionError, SocketError, SocketTimeout, IOError):
            with LM:
                LM.marquee_start('not connected')

def wakeup():
    global snoozetimer
    snoozetimer = None
    with MPD:
        try:
            MPD.play() # player change display handled by idleloop
        except (ConnectionError, SocketError, SocketTimeout, IOError):
            with LM:
                LM.marquee_start('not connected')

def snooze(event):
    global snoozetimer
    with MPD:
        try:
            MPD.stop() # player change display handled by idleloop
        except (ConnectionError, SocketError, SocketTimeout, IOError):
            with LM:
                LM.marquee_start('not connected')
    snoozetimer = threading.Timer(float(config['preferences'].
                             getfloat('snooze_interval'))*60.0,
                             wakeup)
    snoozetimer.start()

def current_pl (event):
    with MPD:
        try:
            status = MPD.status()
            if event.ir_code == 'next' and 'nextsong' in status:
                MPD.next() # player change display handled by idleloop
            elif event.ir_code == 'prev':
                MPD.previous() # player change display handled by idleloop
            elif event.ir_code == 'disp' and 'songid' in status:
                song = MPDSongEntry(MPD.playlistid(status['songid'])[0])
                logging.debug(str(song.entry))
                line2 = status['time'] if 'time' in status else ' '
                line2 = line2 + ' vol: ' + status['volume']+'%'
##                line2 = line2 + ' ' + song.album()
                line2 = line2 + ' ' + song.artist()
                if LM.acquire(False): # display only if LM not locked
                    LM.marquee_start(song.title(), line2)
                    LM.release()
            else:
                if LM.acquire(False): # display only if LM not locked
                    LM.marquee_start('no play list')
                    LM.release()
        except (ConnectionError, SocketError, SocketTimeout, IOError):
            if LM.acquire(False):# display only if LM not locked
                LM.marquee_start('not connected')
                LM.release()

def main():
    global listener
    global idlethread
    global end_barrier
    parser = argparse.ArgumentParser(description='piface IR remote handler for mpd')
    parser.add_argument('-d', '--daemon',
                        action='store_true',
                        help='run as a daemon in the background')
    parser.add_argument('-v', '--verbose',
                        action='count',
                        help='increase log verbosity -v, -vv')
    args = parser.parse_args()
    log_level = config['staticpreferences'].get('log_level','WARNING')
    if args.verbose:
        if args.verbose == 1:
            log_level = 'INFO'
        elif args.verbose >= 2:
            log_level = 'DEBUG'
    if args.daemon:
        pydaemon.createDaemon() # after this call we are a daemon
        logging.basicConfig(
            filename=os.path.expanduser(
                config['staticpreferences'].get('log_file','~/.mpdremote.log')),
            format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
            level=log_level)
    else:
        logging.basicConfig(
            level=log_level)
    
    for key in config:
        logging.debug('['+key+']')
        for key2 in config[key]:
            logging.debug('    '+key2+" = "+config[key][key2])

    end_barrier = Barrier(2)
##  FSM for menu functions
    FSM = Fsm()
    FSM.add_state(State('idle', 'Idle')
                  .add_enterhandlers([
                     # display what is currently playing
                     lambda ev, prev, nxt: LM.marquee(
                         mpdcurrplaylist.updatelist().
                         songentry().
                         title_album()),
                     lambda ev, prev, nxt: LM.release(),
                     lambda ev, prev, nxt: logging.debug('released LCD menu lock'),
                     ])
                  .add_eventhandler('menu', 'playqueue',[
                      lambda ev, prev, nxt: logging.debug('acquiring LCD menu lock'),
                      lambda ev, prev, nxt: LM.acquire(),
                      ])
    )
    FSM.add_state(State('playqueue', 'Play Queue')
                  .add_enterhandlers([
                      lambda ev, prev, nxt: LM.marquee_start('Playqueue>',
                                                          [left,updown,right]),])
                  .add_eventhandler('return', 'idle')
                  .add_eventhandler('left', 'idle')
                  .add_eventhandler('up', 'preferences')
                  .add_eventhandler('down', 'playlists')
                  .add_eventhandler('right', 'songselect', [
                      lambda ev, prev, nxt:
                      mpdcurrplaylist.updatelist(),
                      ]))
    FSM.add_state(State('songselect', 'Song n')
                  .add_enterhandlers([
                      lambda ev, prev, nxt:
                      LM.marquee_start(mpdcurrplaylist.song(),
                                       [left,updown,ok,'-play 2-remove']),
                      ])
                  .add_eventhandler('return', 'playqueue')
                  .add_eventhandler('left', 'playqueue')
                  .add_eventhandler('up', 'songselect',[
                      lambda ev, prev, nxt:
                      mpdcurrplaylist.upsong(),
                      ])
                  .add_eventhandler('down', 'songselect',[
                      lambda ev, prev, nxt:
                      mpdcurrplaylist.downsong(),
                      ])
                  .add_eventhandler('select', 'idle',[
                      lambda ev, prev, nxt:
                      mpdcurrplaylist.selectsong(),
                      ])
                  .add_eventhandler('1key', 'idle',[
                      lambda ev, prev, nxt:
                      mpdcurrplaylist.selectsong(),
                      ])
                  .add_eventhandler('2key', 'songselect',[
                      lambda ev, prev, nxt:
                      mpdcurrplaylist.deletesong(),
                      ]))
    FSM.add_state(State('playlists', 'Play Lists')
                  .add_enterhandlers([
                      lambda ev, prev, nxt:
                      LM.marquee_start('Playlists>',[left,updown,right]),])
                  .add_eventhandler('return', 'idle')
                  .add_eventhandler('left', 'idle')
                  .add_eventhandler('up', 'playqueue')
                  .add_eventhandler('down', 'database')
                  .add_eventhandler('right', 'plsselect', [
                      lambda ev, prev, nxt:
                      mpdplaylists.refresh(),
                      ]))
    FSM.add_state(State('plsselect', 'Playlist n')
                  .add_enterhandlers([
                      lambda ev, prev, nxt:
                      LM.marquee_start(mpdplaylists.pls(),
                                       [left,updown,right,ok,'-repl 2-add']),
                      ])
                  .add_eventhandler('return', 'playlists')
                  .add_eventhandler('left', 'playlists')
                  .add_eventhandler('up', 'plsselect',[
                      lambda ev, prev, nxt:
                      mpdplaylists.uppls(),
                      ])
                  .add_eventhandler('down', 'plsselect',[
                      lambda ev, prev, nxt:
                      mpdplaylists.downpls(),
                      ])
                  .add_eventhandler('right', 'playlistview',[
                      lambda ev, prev, nxt:
                      mpdplaylist.fetch(mpdplaylists.pls()),
                      ])
                  .add_eventhandler('select', 'idle',[
                      lambda ev, prev, nxt:
                      mpdplaylists.selectpls(),
                      ])
                  .add_eventhandler('1key', 'idle',[
                      lambda ev, prev, nxt:
                      mpdplaylists.selectpls(),
                      ])
                  .add_eventhandler('2key', 'idle',[
                      lambda ev, prev, nxt:
                      mpdplaylists.addpls(),
                      ]))
    FSM.add_state(State('playlistview', 'Review a Playlist')
                  .add_enterhandlers([
                      lambda ev,prev, nxt:
                      LM.marquee_start(mpdplaylist.title(),
                                       [left,updown,ok,'-add 2-play']),
                      ])
                  .add_eventhandler('up', 'playlistview',[
                      lambda ev, prev, nxt:
                      mpdplaylist.uptitle(),
                      ])
                  .add_eventhandler('down', 'playlistview',[
                      lambda ev, prev, nxt:
                      mpdplaylist.downtitle(),
                      ])
                  .add_eventhandler('return', 'plsselect')
                  .add_eventhandler('left', 'plsselect')
                  .add_eventhandler('select', 'plsselect',[
                      lambda ev, prev, nxt:
                      mpdplaylist.select(),
                      ])
                  .add_eventhandler('1key', 'plsselect',[
                      lambda ev, prev, nxt:
                      mpdplaylist.select(),
                      ])
                  .add_eventhandler('2key', 'idle',[
                      lambda ev, prev, nxt:
                      mpdplaylist.select(True),
                      ])
                  )
    FSM.add_state(State('database', 'Database Menu')
                  .add_enterhandlers([
                      lambda ev, prev, nxt: LM.marquee_start('Database>',
                                                          [left,updown,right]),])
                  .add_eventhandler('return', 'idle')
                  .add_eventhandler('left', 'idle')
                  .add_eventhandler('up', 'playlists')
                  .add_eventhandler('down', 'modemenus')
                  .add_eventhandler('right', 'DBbrowse', [
                      lambda ev, prev, nxt:
                      mpddatabase.refresh(),
                      ]))
    FSM.add_state(State('DBbrowse', 'Browse Database')
                  .add_enterhandlers([
                      lambda ev, prev, nxt:
                      LM.marquee(mpddatabase.entry()),
                      ])
                  .add_eventhandler('return', 'database')
                  .add_eventhandler('left', 'DBbrowse',[
                      lambda ev, prev, nxt:
                      mpddatabase.back(),
                      ])
                  .add_eventhandler('up', 'DBbrowse',[
                      lambda ev, prev, nxt:
                      mpddatabase.upentry(),
                      ])
                  .add_eventhandler('down', 'DBbrowse',[
                      lambda ev, prev, nxt:
                      mpddatabase.downentry(),
                      ])
                  .add_eventhandler('right', 'DBbrowse', [
                      lambda ev, prev, nxt:
                      mpddatabase.enter(),
                      ])
                  .add_eventhandler('select', 'DBbrowse',[
                      lambda ev, prev, nxt:
                      mpddatabase.select(),
                      ])
                  .add_eventhandler('1key', 'DBbrowse',[
                      lambda ev, prev, nxt:
                      mpddatabase.select(),
                      ])
                  .add_eventhandler('2key', 'idle',[
                      lambda ev, prev, nxt:
                      mpddatabase.select(True),
                      ]))
    FSM.add_state(State('modemenus', 'Mode Menu')
                  .add_enterhandlers([
                      lambda ev,prev, nxt:
                      LM.marquee_start('Mode Menus>', [left,updown,right]),])
                  .add_eventhandler('return', 'idle')
                  .add_eventhandler('left', 'idle')
                  .add_eventhandler('up', 'database',)
                  .add_eventhandler('down', 'preferences')
                  .add_eventhandler('right', 'randommode'))
    FSM.add_state(State('randommode', 'Random Mode')
                  .add_enterhandlers([
                      lambda ev, prev, nxt:
                      LM.marquee_start('Random '+mpdstatus.random(),
                                       [left,updown,ok,'-toggles']),])
                  .add_eventhandler('return', 'modemenus')
                  .add_eventhandler('left', 'modemenus')
                  .add_eventhandler('up', 'singlemode')
                  .add_eventhandler('down', 'consumemode')
                  .add_eventhandler('select', 'randommode', [
                      lambda ev, prev, nxt: mpdstatus.random(True),
                      ]))
    FSM.add_state(State('consumemode', 'Consume Mode')
                  .add_enterhandlers([
                      lambda ev, prev, nxt:
                      LM.marquee_start('Consume '+mpdstatus.consume(),
                                       [left,updown,ok,'-toggles']),])
                  .add_eventhandler('return', 'modemenus')
                  .add_eventhandler('left', 'modemenus')
                  .add_eventhandler('up', 'randommode')
                  .add_eventhandler('down', 'repeatmode')
                  .add_eventhandler('select', 'consumemode', [
                      lambda ev, prev, nxt: mpdstatus.consume(True),
                      ]))
    FSM.add_state(State('repeatmode', 'Repeat Mode')
                  .add_enterhandlers([
                      lambda ev, prev, nxt:
                      LM.marquee_start('Repeat '+mpdstatus.repeat(),
                                       [left,updown,ok,'-toggles']),])
                  .add_eventhandler('return', 'modemenus')
                  .add_eventhandler('left', 'modemenus')
                  .add_eventhandler('up', 'consumemode')
                  .add_eventhandler('down', 'singlemode')
                  .add_eventhandler('select', 'repeatmode', [
                      lambda ev, prev, nxt: mpdstatus.repeat(True),
                      ]))
    FSM.add_state(State('singlemode', 'Single Mode')
                  .add_enterhandlers([
                      lambda ev, prev, nxt:
                      LM.marquee_start('Single '+mpdstatus.single(),
                                       [left,updown,ok,'-toggles']),])
                  .add_eventhandler('return', 'modemenus')
                  .add_eventhandler('left', 'modemenus')
                  .add_eventhandler('up', 'repeatmode')
                  .add_eventhandler('down', 'randommode')
                  .add_eventhandler('select', 'singlemode', [
                      lambda ev, prev, nxt: mpdstatus.single(True),
                      ]))
    FSM.add_state(State('preferences', 'Preferences')
                  .add_enterhandlers([
                      lambda ev,prev, nxt:
                      LM.marquee_start('Preferences>',
                                       [left,updown,right]),])
                  .add_eventhandler('return', 'idle')
                  .add_eventhandler('left', 'idle')
                  .add_eventhandler('up', 'modemenus')
                  .add_eventhandler('down', 'playqueue')
                  .add_eventhandler('right', 'preferencemenus', [
                      lambda ev, prev, nxt: MENUS.refresh(),
                      ]))
    FSM.add_state(State('preferencemenus', 'Preference Menus')
                  .add_enterhandlers([
                      lambda ev,prev, nxt:
                      LM.marquee(MENUS.show()),
                                       ])
                  .add_eventhandler('return', 'preferences')
                  .add_eventhandler('left', 'preferences')
                  .add_eventhandler('up', 'preferencemenus',[
                      lambda ev,prev, nxt:
                      MENUS.up(),
                      ])
                  .add_eventhandler('down', 'preferencemenus',[
                      lambda ev,prev, nxt:
                      MENUS.down(),
                      ])
                  .add_eventhandler('right', 'choicemenu', [
                      lambda ev,prev, nxt:
                      MENUS.startchoice(),
                      ]))
    FSM.add_state(State('choicemenu', 'Choice Menu')
                  .add_enterhandlers([
                      lambda ev,prev, nxt:
                      LM.marquee(MENUS.showchoice()),
                                       ])
                  .add_eventhandler('return', 'preferencemenus')
                  .add_eventhandler('left', 'preferencemenus')
                  .add_eventhandler('up', 'choicemenu',[
                      lambda ev,prev, nxt:
                      MENUS.upchoice(),
                      ])
                  .add_eventhandler('down', 'choicemenu',[
                      lambda ev,prev, nxt:
                      MENUS.downchoice(),
                      ])
                  .add_eventhandler('select', 'preferencemenus', [
                      lambda ev,prev, nxt:
                      MENUS.setchoice(),
                      ]))
    FSM.start('idle')
    ## MPD object instance
    CAD.lcd.blink_off()
    CAD.lcd.cursor_off()
    MPD.timeout = 15
    connect_client(MPD,'MPD')
    ping()
    idlethread = Thread(target=idleloop)
    idlethread.daemon = True
    MPD2.timeout = 15
    connect_client(MPD2,'MPD2')
    idlethread.start()
    infolooper()
    
    listener = pifacecad.IREventListener(prog="mpdremote")
    listener.register('volumeup', lambda ev: mpdstatus.volume(ev.ir_code))
    listener.register('volumedown', lambda ev: mpdstatus.volume(ev.ir_code))
    listener.register('advance', lambda ev: mpdstatus.time(ev.ir_code))
    listener.register('replay', lambda ev: mpdstatus.time(ev.ir_code))
    listener.register('next', current_pl)
    listener.register('prev', current_pl)
    listener.register('disp', current_pl)
    listener.register('power', power_off)
    listener.register('play', play)
    listener.register('pause', pause)
    listener.register('stop', stop)
    listener.register('snooze', snooze)
    listener.register('menu', lambda ev: FSM.execute(ev.ir_code))
    listener.register('return', lambda ev: FSM.execute(ev.ir_code))
    listener.register('left', lambda ev: FSM.execute(ev.ir_code))
    listener.register('right', lambda ev: FSM.execute(ev.ir_code))
    listener.register('up', lambda ev: FSM.execute(ev.ir_code))
    listener.register('down', lambda ev: FSM.execute(ev.ir_code))
    listener.register('select', lambda ev: FSM.execute(ev.ir_code))
    listener.register('1key', lambda ev: FSM.execute(ev.ir_code))
    listener.register('2key', lambda ev: FSM.execute(ev.ir_code))
    listener.activate()
    logging.debug('ir listener activated, waiting on barrier')
    end_barrier.wait()  # wait unitl exit
    logging.debug('deactivating listener')
    listener.deactivate()
    CAD.lcd.backlight_off()
    CAD.lcd.clear()
    CAD.lcd.write('bye')
    logging.info('bye')

# Script starts here
if __name__ == "__main__":
    main()
