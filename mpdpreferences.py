#! /usr/bin/python3
import os
import subprocess
import logging
import configparser

logger = logging.getLogger('mpdmanager')

class MpdPreferences:
    def __init__(self):
        self.mpd_services_list = []
        
# Need avahi-utils installed
        try:
            logger.debug('starting avahi search for mpd servers')
            mpd_services = subprocess.check_output(['avahi-browse','-tkrfp','_mpd._tcp'],
                                                   universal_newlines=True)
## mpd_services should look like this
## +;wlan0;IPv4;PiMusic\032Player;_mpd._tcp;local
## =;wlan0;IPv4;PiMusic\032Player;_mpd._tcp;local;walden9.local;192.168.1.108;6600;
            logger.debug('avahi search returned: \n'+mpd_services)
            mpd_services = mpd_services.splitlines()
            for mpd_service in mpd_services:
                mpd_service_list = mpd_service.split(";")
                if mpd_service_list[0] == "=":
                    logger.debug('found mpd service: '+mpd_service_list[3])
                    self.mpd_services_list.append({'name':mpd_service_list[3].replace("\\032"," "),
                             'host':mpd_service_list[6],
                             'address':mpd_service_list[7],
                             'port':mpd_service_list[8]})
        except OSError as ose:
            logger.debug(str(ose))
            logger.warning('avahi utility avahi-browse not found.')
            logger.info('Will fallback to staticpreferences to locate an mpd server.')
        except subprocess.CalledProcessError as sbe:
            logger.debug(str(sbe))
            logger.warning('avahi utility avahi-browse error.')
            logger.info('Will fallback to staticpreferences to locate an mpd server.')
        
        # add local host option
        self.mpd_services_list.append({'name':'localhost',
                                       'host':'localhost',
                                       'address':'127.0.0.1',
                                       'port':'6600'})

    def mpdnames(self):
        mpdnamelist = []
        for item in self.mpd_services_list:
            mpdnamelist.append(item['name'])
        return mpdnamelist

    def preferredClient(self, config):
        mpdname = config['preferences'].get('preferredmpd')
        if mpdname:
            try:
                mpdrec = next(d for (index, d) in enumerate(self.mpd_services_list) if d["name"] == mpdname)
                return mpdrec
            except StopIteration:
                pass
        return {'name':mpdname,'host':None,'address':None,'port':None}

# test script starts here
if __name__ == "__main__":
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

    mpdmgr = MpdPreferences()
    print(mpdmgr.mpd_services_list)
    print(mpdmgr.preferredClient(config))
    print(mpdmgr.mpdnames())
