

"""
Driver for Aceinna 380/381 Series Products
Based on PySerial https://github.com/pyserial/pyserial
Created on 2017-10-01
@author: m5horton
"""

import serial
import math
import string
import quat
import time
import sys
import aceinna_storage
import file_storage
import collections
import glob

class GrabIMU380Data:
    def __init__(self):
        '''Initialize and then start ports search and autobaud process
        '''
        self.ser = None             # the active UART
        self.stream_mode = 0        # 0 = polled, 1 = streaming
        self.device_id = 0          # unit's id str
        self.odr_setting = 0        # hex code of the ODR field (0x0001)
        self.logging = 0            # logging on or off
        self.logger = None          # the file or cloud logger
        self.packet_size = 0        
        self.packet_type = 0        
        self.data = {}              # placeholder imu measurements of last converted packeted
        
        # find the IMU  380
        self.find_device()


    def find_device(self):
        ''' Finds active ports and then autobauds units, repeats every 2 seconds
        '''
        while not self.autobaud(self.find_ports()):
            time.sleep(2)

    def find_ports(self):
        ''' Lists serial port names. Code from
            https://stackoverflow.com/questions/12090503/listing-available-com-ports-with-python
            Successfully tested on Windows 8.1 x64, Windows 10 x64, Mac OS X 10.9.x / 10.10.x / 10.11.x and Ubuntu 14.04 / 14.10 / 15.04 / 15.10 with both Python 2 and Python 3.
            :raises EnvironmentError:
                On unsupported or unknown platforms
            :returns:
                A list of the serial ports available on the system
        '''
        print('scanning ports')
        if sys.platform.startswith('win'):
            ports = ['COM%s' % (i + 1) for i in range(256)]
        elif sys.platform.startswith('linux') or sys.platform.startswith('cygwin'):
            # this excludes your current terminal "/dev/tty"
            ports = glob.glob('/dev/tty[A-Za-z]*')
        elif sys.platform.startswith('darwin'):
            ports = glob.glob('/dev/tty.*')
        else:
            raise EnvironmentError('Unsupported platform')

        result = []
        for port in ports:
            try:
                print('Trying: ' + port)
                s = serial.Serial(port)
                s.close()
                result.append(port)
            except (OSError, serial.SerialException):
                pass
        return result

    def autobaud(self, ports):
        '''Autobauds unit - first check for stream_mode / continuous data, then check by polling unit
           Converts resets polled unit (temporarily) to 100Hz ODR
           :returns: 
                true when successful
        ''' 
        
        for port in ports:
            for baud in [115200, 57600, 38400, 19200, 9600]:
                self.ser = serial.Serial(port, baud, timeout = 0.1)
                # sync() works for stream mode
                self.sync()
                if self.stream_mode:
                    print('Connected Stream Mode ' + '{0:d}'.format(baud) + '  ' + port)
                    break
                else:
                    self.ser.close()
            
            # stream mode not found for port, check port by polling
            if self.stream_mode == 0:  
                for baud in [115200, 57600, 38400, 19200, 9600]:
                    self.ser = serial.Serial(port, baud, timeout = 0.1)
                    self.device_id = self.get_id_str()
                    if self.device_id:
                        print('Connected Polled Mode ' + '{0:d}'.format(baud))
                        # return it to streamed mode
                        self.odr_setting = 0x01
                        self.set_fields([[0x0001, self.odr_setting]])
                        self.sync()        
                        print('Now Connectd Stream Mode ' + '{0:d}'.format(baud))                
                    else:
                        self.ser.close()

            # in stream stream mode worked, get odr field and id str
            else:
                odr = self.read_fields([0x0001])
                if odr:
                    self.odr_setting = sum(odr[3:5])  # read ODR field
                    self.device_id = self.get_id_str()
                    return True
                else:
                    print('failed to get id string')
                    return False
        
        return False

    def get_latest(self):
        '''Get latest converted IMU readings in converted units
            :returns:
                data object or error message for web socket server to pass to app
        '''
        if self.stream_mode == 1:
            return self.data
        else: 
            return { 'err' : 'Not connected' }
    
    def start_log(self, type = False, ws = False):
        '''Creates file or cloud logger.  Autostarts log activity if ws (websocket) set to false
        '''
        self.logging = 1
        if (type == 'cloud'):
            self.logger = aceinna_storage.LogIMU380Data()
        else:
            self.logger = file_storage.LogIMU380Data()
        if ws == False and self.odr_setting != 0:
            self.stream()
    
    def stop_log(self, ws = False):
        '''Stops file or cloud logger
        '''
        self.logging = 0
        self.logger.close()
        self.logger = None

    def ping_test(self):
        '''Executes ping test.  Not currently used
            :returns:
                True is successful
        '''

        self.stream_mode = 0      
        C = [0x55, 0x55, 0x50, 0x4B, 0x00]      # 0x55504B00
        crc = self.calc_crc(C[2:4] + [0x00])    # for some reason must add a payload byte to get correct CRC
        crc_msb = (crc & 0xFF00) >> 8
        crc_lsb = (crc & 0x00FF)
        C.insert(len(C), crc_msb)
        C.insert(len(C), crc_lsb)
        self.ser.reset_input_buffer()
        self.ser.write(C)
        R = self.ser.read(7)                    # grab with header, type, length, and crc
        if bytearray(R) == bytearray(C):
            return True
        else: 
            return False
    
    def get_fields(self,fields): 
        '''Executes 380 GF command for an array of fields.  GF Command get current Temporary setting of 380
        ''' 
        # Take unit out of stream mode
        self.set_quiet()     
        num_fields = len(fields)
        C = [0x55, 0x55, ord('G'), ord('F'), num_fields * 2 + 1, num_fields]
        for field in fields:
            field_msb = (field & 0xFF00)  >> 8
            field_lsb = field & 0x00FF  
            C.insert(len(C), field_msb)
            C.insert(len(C), field_lsb)
        crc = self.calc_crc(C[2:C[4]+5])   
        crc_msb = (crc & 0xFF00) >> 8
        crc_lsb = (crc & 0x00FF)
        C.insert(len(C), crc_msb)
        C.insert(len(C), crc_lsb)
        self.ser.write(C)
        R = bytearray(self.ser.read(num_fields * 4 + 1 + 7))
        if R[0] == 85 and R[1] == 85:
            packet_crc = 256 * R[-2] + R[-1]                   # crc is last two bytes
            calc_crc = self.calc_crc(R[2:R[4]+5])
            if packet_crc == calc_crc:
                self.packet_type = '{0:1c}'.format(R[2]) + '{0:1c}'.format(R[3])
                self.parse_packet(R[5:R[4]+5])
    
    def read_fields(self,fields): 
        '''Executes 380 RF command for an array of fields.  RF Command get current Permanent setting of 380
        ''' 
        # Take unit out of stream mode
        self.set_quiet()     
        num_fields = len(fields)
        C = [0x55, 0x55, ord('R'), ord('F'), num_fields * 2 + 1, num_fields]
        for field in fields:
            field_msb = (field & 0xFF00)  >> 8
            field_lsb = field & 0x00FF  
            C.insert(len(C), field_msb)
            C.insert(len(C), field_lsb)
        crc = self.calc_crc(C[2:C[4]+5])   
        crc_msb = (crc & 0xFF00) >> 8
        crc_lsb = (crc & 0x00FF)
        C.insert(len(C), crc_msb)
        C.insert(len(C), crc_lsb)
        self.ser.write(C)
        R = bytearray(self.ser.read(num_fields * 4 + 1 + 7))
        print(R)
        if len(R) and R[0] == 85 and R[1] == 85:
            packet_crc = 256 * R[-2] + R[-1]                   # crc is last two bytes
            calc_crc = self.calc_crc(R[2:R[4]+5])
            if packet_crc == calc_crc:
                self.packet_type = '{0:1c}'.format(R[2]) + '{0:1c}'.format(R[3])
                return self.parse_packet(R[5:R[4]+5])
    
    def write_fields(self, field_value_pairs):
        '''Executes 380 WF command for an array of fields, value pairs.  WF Command set Permanent setting for fields on 380
        '''
        self.set_quiet()     
        num_fields = len(field_value_pairs)
        C = [0x55, 0x55, ord('W'), ord('F'), num_fields * 4 + 1 , num_fields]
        FIELD = 0
        VALUE = 1
        for field_value in field_value_pairs:
            field_msb = (field_value[FIELD] & 0xFF00)  >> 8
            field_lsb = field_value[FIELD] & 0x00FF  
            if isinstance(field_value[VALUE], int):
                value_msb = (field_value[VALUE] & 0xFF00) >> 8
                value_lsb = field_value[VALUE] & 0x0FF
            elif isinstance(field_value[VALUE], str):
                value_msb = ord(field_value[VALUE][0])
                value_lsb = ord(field_value[VALUE][1])
            C.insert(len(C), field_msb)
            C.insert(len(C), field_lsb)
            C.insert(len(C), value_msb)
            C.insert(len(C), value_lsb)
        crc = self.calc_crc(C[2:C[4]+5])   
        crc_msb = (crc & 0xFF00) >> 8
        crc_lsb = (crc & 0x00FF)
        C.insert(len(C), crc_msb)
        C.insert(len(C), crc_lsb)
        self.ser.write(C)
        R = bytearray(self.ser.read(num_fields * 2 + 1 +7))
        if R[0] == 85 and R[1] == 85:
            packet_crc = 256 * R[-2] + R[-1]                   # crc is last two bytes
            if self.calc_crc(R[2:R[4]+5]) == packet_crc:
                if R[2] == 0 and R[3] == 0:
                    print('SET FIELD ERROR/FAILURE')
                    return
                else: 
                    self.packet_type =  '{0:1c}'.format(R[2]) + '{0:1c}'.format(R[3])
                    self.parse_packet(R[5:R[4]+5])
    
    def set_fields(self, field_value_pairs):
        '''Executes 380 SF command for an array of fields, value pairs.  SF Command sets Temporary setting for fields on 380
        '''
        self.set_quiet()     
        num_fields = len(field_value_pairs)
        C = [0x55, 0x55, ord('S'), ord('F'), num_fields * 4 + 1 , num_fields]
        FIELD = 0
        VALUE = 1
        for field_value in field_value_pairs:
            field_msb = (field_value[FIELD] & 0xFF00)  >> 8
            field_lsb = field_value[FIELD] & 0x00FF  
            value_msb = (field_value[VALUE] & 0xFF00) >> 8
            value_lsb = field_value[VALUE] & 0x0FF
            C.insert(len(C), field_msb)
            C.insert(len(C), field_lsb)
            C.insert(len(C), value_msb)
            C.insert(len(C), value_lsb)
        crc = self.calc_crc(C[2:C[4]+5])   
        crc_msb = (crc & 0xFF00) >> 8
        crc_lsb = (crc & 0x00FF)
        C.insert(len(C), crc_msb)
        C.insert(len(C), crc_lsb)
        self.ser.write(C)
        R = bytearray(self.ser.read(num_fields * 2 + 1 +7))
        if R[0] == 85 and R[1] == 85:
            packet_crc = 256 * R[-2] + R[-1]                   # crc is last two bytes
            if self.calc_crc(R[2:R[4]+5]) == packet_crc:
                if R[2] == 0 and R[3] == 0:
                    print('SET FIELD ERROR/FAILURE')
                    return
                else: 
                    self.packet_type =  '{0:1c}'.format(R[2]) + '{0:1c}'.format(R[3])
                    self.parse_packet(R[5:R[4]+5])
        
    def set_quiet(self):
        '''Force 380 device to quiet / polled mode and inject 0.1 second delay, then clear input buffer
        '''
        self.stream_mode = 0
        C = [0x55, 0x55, ord('S'), ord('F'), 0x05 , 0x01, 0x00, 0x01, 0x00, 0x00]
        crc = self.calc_crc(C[2:C[4]+5])   
        crc_msb = (crc & 0xFF00) >> 8
        crc_lsb = (crc & 0x00FF)
        C.insert(len(C), crc_msb)
        C.insert(len(C), crc_lsb)
        self.ser.reset_input_buffer()
        self.ser.write(C)
        self.ser.read(10)
        time.sleep(0.1) # wait for command to take effect
        self.ser.reset_input_buffer()
    
    def stream(self):
        '''Set 380 to odr_setting and connect.  Assume find_device has already occured at some prior point
        ''' 
        if self.odr_setting:
            self.set_fields([[0x0001, self.odr_setting]])
            self.connect()
        
    def connect(self):
        '''Continous data collection loop to get and process data packets in stream mode
        '''
        self.connected = 1
        while self.connected:
            self.get_packet()
        return  # ends thread

    def disconnect(self):
        '''Ends data collection loop
        '''
        self.connected = 0

    def get_packet(self):
        '''Syncs unit and gets packet.  Assumes unit is in stream_mode'''

        # Already synced
        if self.stream_mode == 1:    
            # Read next packet of data based on expected packet size     
            S = bytearray(self.ser.read(self.packet_size + 7))   
 
            if not len(S):
                # Read Failed
                self.stream_mode = 0                    
                return
            if S[0] == 85 and S[1] == 85:
                packet_crc = 256 * S[-2] + S[-1]    
                # Compare computed and read crc               
                if self.calc_crc(S[2:S[4]+5]) == packet_crc: 
                    # 5 is offset of first payload byte, S[4]+5 is offset of last payload byte     
                    self.data = self.parse_packet(S[5:S[4]+5])     
            else: 
                # Get synced and then read next packet
                self.sync()
                self.get_packet()
        else:
            # Get synced and then read next packet
            self.sync()
            self.get_packet()

    def sync(self,prev_byte = 0,bytes_read = 0):
        '''Syncs a 380 in Continuous / Stream mode.  Assumes longest packet is 40 bytes
            TODO: check this assumption
            TODO: add check of CRC
            :returns:
                true if synced, false if not
        '''
        try:    
            S = bytearray(self.ser.read(1))
        except serial.SerialException:
            S = []
            print('serial exception')
            self.find_device()

        if not len(S):
            return False
        if S[0] == 85 and prev_byte == 85:      # VALID HEADER FOUND
            # Once header is found then read off the rest of packet
            self.stream_mode = 1
            config_bytes = bytearray(self.ser.read(3))
            self.packet_type = '{0:1c}'.format(config_bytes[0]) + '{0:1c}'.format(config_bytes[1])
            self.packet_size = config_bytes[2]
            bytearray(self.ser.read(config_bytes[2] + 2))      # clear bytes off port, payload + 2 byte CRC
            return True
        else: 
            # Repeat sync to search next byte pair for header
            if bytes_read == 0:
                print('Connecting ....')
            bytes_read = bytes_read + 1
            self.stream_mode = 0
            if (bytes_read < 40):
                self.sync(S[0], bytes_read)
            else:
                return False
    
    def start_bootloader(self):
        '''Starts bootloader
            :returns:
                True if bootloader mode entered, False if failed
        '''
        self.set_quiet()
        C = [0x55, 0x55, ord('J'), ord('I'), 0x00 ]
        crc = self.calc_crc(C[2:4] + [0x00])    # for some reason must add a payload byte to get correct CRC
        crc_msb = (crc & 0xFF00) >> 8
        crc_lsb = (crc & 0x00FF)
        C.insert(len(C), crc_msb)
        C.insert(len(C), crc_lsb)
        self.ser.write(C)
        time.sleep(1)   # must wait for boot loader to be ready
        R = bytearray(self.ser.read(5)) 
        if R[0] == 85 and R[1] == 85:
            self.packet_type =  '{0:1c}'.format(R[2]) + '{0:1c}'.format(R[3])
            if self.packet_type == 'JI':
                self.ser.read(R[4]+2)
                print('bootloader ready')
                time.sleep(2)
                self.ser.reset_input_buffer()
                return True
            else: 
                return False
        else:
            return False
    
    def start_app(self):
        '''Starts app
        '''
        self.set_quiet()
        C = [0x55, 0x55, ord('J'), ord('A'), 0x00 ]
        crc = self.calc_crc(C[2:4] + [0x00])    # for some reason must add a payload byte to get correct CRC
        crc_msb = (crc & 0xFF00) >> 8
        crc_lsb = (crc & 0x00FF)
        C.insert(len(C), crc_msb)
        C.insert(len(C), crc_lsb)
        self.ser.write(C)
        time.sleep(1)
        R = bytearray(self.ser.read(7))    
        if R[0] == 85 and R[1] == 85:
            self.packet_type =  '{0:1c}'.format(R[2]) + '{0:1c}'.format(R[3])
            print(self.packet_type)

    def write_block(self, buf, data_len, addr):
        '''Executed WA command to write a block of new app code into memory
        '''
        print(data_len, addr);
        C = [0x55, 0x55, ord('W'), ord('A'), data_len+5]
        addr_3 = (addr & 0xFF000000) >> 24
        addr_2 = (addr & 0x00FF0000) >> 16
        addr_1 = (addr & 0x0000FF00) >> 8
        addr_0 = (addr & 0x000000FF)
        C.insert(len(C), addr_3)
        C.insert(len(C), addr_2)
        C.insert(len(C), addr_1)
        C.insert(len(C), addr_0)
        C.insert(len(C), data_len)
        for i in range(data_len):
            C.insert(len(C), ord(buf[i]))
        crc = self.calc_crc(C[2:C[4]+5])  
        crc_msb = int((crc & 0xFF00) >> 8)
        crc_lsb = int((crc & 0x00FF))
        C.insert(len(C), crc_msb)
        C.insert(len(C), crc_lsb)
        status = 0
        while (status == 0):
            self.ser.write(C)
            if addr == 0:
               time.sleep(10)
            R = bytearray(self.ser.read(12))  #longer response
            if len(R) > 1 and R[0] == 85 and R[1] == 85:
                self.packet_type =  '{0:1c}'.format(R[2]) + '{0:1c}'.format(R[3])
                print(self.packet_type)
                if self.packet_type == 'WA':
                    status = 1
                else:
                    sys.exit()
                    print('retry 1')
                    status = 0
            else:
                print(len(R))
                print(R)
                self.ser.reset_input_buffer()
                time.sleep(1)
                print('no packet')
                sys.exit()
        
    def upgrade_fw(self,file):
        '''Upgrades firmware of connected 380 device to file provided in argument
        '''
        max_data_len = 240
        write_len = 0
        fw = open(file, 'rb').read()
        fs_len = len(fw)

        if not self.start_bootloader():
            print('Bootloader Start Failed')
            return False
        
        time.sleep(1)
        while (write_len < fs_len):
            packet_data_len = max_data_len if (fs_len - write_len) > max_data_len else (fs_len-write_len)
            # From IMUView 
            # Array.Copy(buf,write_len,write_buf,0,packet_data_len);
            write_buf = fw[write_len:(write_len+packet_data_len)]
            self.write_block(write_buf, packet_data_len, write_len)
            write_len += packet_data_len
        time.sleep(1)
        # Start new app
        self.start_app()
    
    def get_id_str(self):
        ''' Executes GP command and requests ID data from 380
            :returns:
                id string of connected device, or false if failed
        '''
        self.set_quiet()
        C = [0x55, 0x55, ord('G'), ord('P'), 0x02, ord('I'), ord('D') ]
        crc = self.calc_crc(C[2:C[4]+5])   
        crc_msb = (crc & 0xFF00) >> 8
        crc_lsb = (crc & 0x00FF)
        C.insert(len(C), crc_msb)
        C.insert(len(C), crc_lsb)
        self.ser.write(C)
        R = bytearray(self.ser.read(5))    
        if len(R) and R[0] == 85 and R[1] == 85:
            self.packet_type =  '{0:1c}'.format(R[2]) + '{0:1c}'.format(R[3])
            payload_length = R[4]
            R = bytearray(self.ser.read(payload_length+2))
            id_str = self.parse_packet(R[0:payload_length])
            return id_str
        else: 
            return False



    def parse_packet(self, payload):
        '''Parses packet payload to engineering units based on packet type
           Currently supports S0, S1, A1 packets.  Logs data if logging is on.
           Prints data if a GF/RF/SF/WF
        '''
        if self.packet_type == 'S0':
            '''S0 Payload Contents
                Byte Offset	Name	Format	Scaling	Units	Description
                0	xAccel	    I2	20/2^16	G	X accelerometer
                2	yAccel	    I2	20/2^16	G	Y accelerometer
                4	zAccel	    I2	20/2^16	G	Z accelerometer
                6	xRate   	I2	7*pi/2^16 [1260 deg/2^16]	rad/s [deg/sec]	X angular rate
                8	yRate	    I2	7*pi/2^16 [1260 deg/2^16]	rad/s [deg/sec]	Y angular rate
                10	zRate	    I2	7*pi/2^16 [1260 deg/2^16]	rad/s [deg/sec]	Z angular rate
                12	xMag	    I2	2/2^16	Gauss	X magnetometer
                14	yMag	    I2	2/2^16	Gauss	Y magnetometer
                16	zMag	    I2	2/2^16	Gauss	Z magnetometer
                18	xRateTemp	I2	200/2^16	deg. C	X rate temperature
                20	yRateTemp	I2	200/2^16	deg. C	Y rate temperature
                22	zRateTemp	I2	200/2^16	deg. C	Z rate temperature
                24	boardTemp	I2	200/2^16	deg. C	CPU board temperature
                26	GPSITOW	    U2	truncated	Ms	GPS ITOW (lower 2 bytes)
                28	BITstatus   U2 Master BIT and Status'''
            
            accels = [0 for x in range(3)] 
            for i in range(3):
                accel_int16 = (256 * payload[2*i] + payload[2*i+1]) - 65535 if 256 * payload[2*i] + payload[2*i+1] > 32767  else  256 * payload[2*i] + payload[2*i+1]
                accels[i] = (20 * accel_int16) / math.pow(2,16)
  
            gyros = [0 for x in range(3)] 
            for i in range(3):
                gyro_int16 = (256 * payload[2*i+6] + payload[2*i+7]) - 65535 if 256 * payload[2*i+6] + payload[2*i+7] > 32767  else  256 * payload[2*i+6] + payload[2*i+7]
                gyros[i] = (7 * math.pi * gyro_int16) / math.pow(2,16) 

            mags = [0 for x in range(3)] 
            for i in range(3):
                mag_int16 = (256 * payload[2*i+12] + payload[2*i+13]) - 65535 if 256 * payload[2*i+12] + payload[2*i+13] > 32767  else  256 * payload[2*i+12] + payload[2*i+13]
                mags[i] = (2 * mag_int16) / math.pow(2,16) 

            temps = [0 for x in range(4)] 
            for i in range(4):
                temp_int16 = (256 * payload[2*i+18] + payload[2*i+19]) - 65535 if 256 * payload[2*i+18] + payload[2*i+19] > 32767  else  256 * payload[2*i+18] + payload[2*i+19]
                temps[i] = (200 * temp_int16) / math.pow(2,16)
        
            # Counter Value
            itow = 256 * payload[26] + payload[27]   

            # BIT Value
            bit = 256 * payload[28] + payload[29]         

            data = collections.OrderedDict([( 'xAccel', accels[0]), ('yAccel', accels[1]), ('zAccel', accels[2]), ('xRate', gyros[0]), \
                     ('yRate' , gyros[1]), ('zRate', gyros[2]), ('xMag', mags[0]), ('yMag', mags[1]), ('zMag', mags[2]), ('xRateTemp', temps[0]), \
                     ('yRateTemp', temps[1]), ('zRateTemp', temps[2]), ('boardTemp', temps[3]), ('GPSITOW', itow), ('BITstatus', bit )])


            if self.logging == 1 and self.logger is not None:
                self.logger.log(data, self.odr_setting) 
            
            return data
        elif self.packet_type == 'S1':
            '''S1 Payload Contents
                Byte Offset	Name	Format	Scaling	Units	Description
                0	xAccel	I2	20/2^16	G	X accelerometer
                2	yAccel	I2	20/2^16	G	Y accelerometer
                4	zAccel	I2	20/2^16	G	Z accelerometer
                6	xRate	I2	7*pi/2^16   [1260 deg/2^16]	rad/s [deg/sec]	X angular rate
                8	yRate	I2	7*pi/2^16   [1260 deg/2^16]	rad/s [deg/sec]	Y angular rate
                10	zRate	I2	7*pi/2^16   [1260 deg/2^16]	rad/s [deg/sec]	Z angular rate
                12	xRateTemp	I2	200/2^16	deg. C	X rate temperature
                14	yRateTemp	I2	200/2^16	deg. C	Y rate temperature
                16	zRateTemp	I2	200/2^16	deg. C	Z rate temperature
                18	boardTemp	I2	200/2^16	deg. C	CPU board temperature
                20	counter	U2	-	packets	Output packet counter
                22	BITstatus	U2	-	-	Master BIT and Status'''

            accels = [0 for x in range(3)] 
            for i in range(3):
                accel_int16 = (256 * payload[2*i] + payload[2*i+1]) - 65535 if 256 * payload[2*i] + payload[2*i+1] > 32767  else  256 * payload[2*i] + payload[2*i+1]
                accels[i] = (20 * accel_int16) / math.pow(2,16)
  
            gyros = [0 for x in range(3)] 
            for i in range(3):
                gyro_int16 = (256 * payload[2*i+6] + payload[2*i+7]) - 65535 if 256 * payload[2*i+6] + payload[2*i+7] > 32767  else  256 * payload[2*i+6] + payload[2*i+7]
                gyros[i] = (7 * math.pi * gyro_int16) / math.pow(2,16) 

            temps = [0 for x in range(4)] 
            for i in range(4):
                temp_int16 = (256 * payload[2*i+12] + payload[2*i+13]) - 65535 if 256 * payload[2*i+12] + payload[2*i+13] > 32767  else  256 * payload[2*i+12] + payload[2*i+13]
                temps[i] = (200 * temp_int16) / math.pow(2,16)
        
            # Counter Value
            counter = 256 * payload[20] + payload[21]   

            # BIT Value
            bit = 256 * payload[22] + payload[23]         

            data = collections.OrderedDict([( 'xAccel', accels[0]), ('yAccel', accels[1]), ('zAccel', accels[2]), ('xRate', gyros[0]), \
                     ('yRate' , gyros[1]), ('zRate', gyros[2]), ('xRateTemp', temps[0]), \
                     ('yRateTemp', temps[1]), ('zRateTemp', temps[2]), ('boardTemp', temps[3]), ('counter', counter), ('BITstatus', bit )])


            if self.logging == 1 and self.logger is not None:
                self.logger.log(data, self.odr_setting) 
            
            return data
      
        elif self.packet_type == 'A1': 
            '''A1 Payload Contents
                0	rollAngle	I2	2*pi/2^16 [360 deg/2^16]	Radians [deg]	Roll angle
                2	pitchAngle	I2	2*pi/2^16 [360 deg/2^16]	Radians [deg]	Pitch angle
                4	yawAngleMag	I2	2*pi/2^16 [360 deg/2^16]	Radians [deg]	Yaw angle (magnetic north)
                6	xRateCorrected	I2	7*pi/2^16[1260 deg/2^16]	rad/s  [deg/sec]	X angular rate Corrected
                8	yRateCorrected	I2	7*pi/2^16 [1260 deg/2^16]	rad/s  [deg/sec]	Y angular rate Corrected
                10	zRateCorrected	I2	7*pi/2^16 [1260 deg/2^16]	rad/s  [deg/sec]	Z angular rate Corrected
                12	xAccel	I2	20/2^16	g	X accelerometer
                14	yAccel	I2	20/2^16	g	Y accelerometer
                16	zAccel	I2	20/2^16	g	Z accelerometer
                18	xMag	I2	2/2^16	Gauss	X magnetometer
                20	yMag	I2	2/2^16	Gauss	Y magnetometer
                22	zMag	I2	2/2^16	Gauss	Z magnetometer
                24	xRateTemp	I2	200/2^16	Deg C	X rate temperature
                26	timeITOW	U4	1	ms	DMU ITOW (sync to GPS)
                30	BITstatus	U2	-	-	Master BIT and Status'''

            angles = [0 for x in range(3)] 
            for i in range(3):
                angle_int16 = (256 * payload[2*i] + payload[2*i+1]) - 65535 if 256 * payload[2*i] + payload[2*i+1] > 32767  else  256 * payload[2*i] + payload[2*i+1]
                angles[i] = (2 * math.pi * angle_int16) / math.pow(2,16) 

            gyros = [0 for x in range(3)] 
            for i in range(3):
                gyro_int16 = (256 * payload[2*i+6] + payload[2*i+7]) - 65535 if 256 * payload[2*i+6] + payload[2*i+7] > 32767  else  256 * payload[2*i+6] + payload[2*i+7]
                gyros[i] = (7 * math.pi * gyro_int16) / math.pow(2,16) 

            accels = [0 for x in range(3)] 
            for i in range(3):
                accel_int16 = (256 * payload[2*i+12] + payload[2*i+1+13]) - 65535 if 256 * payload[2*i+12] + payload[2*i+13] > 32767  else  256 * payload[2*i+12] + payload[2*i+13]
                accels[i] = (20 * accel_int16) / math.pow(2,16)
            
            mags = [0 for x in range(3)] 
            for i in range(3):
                mag_int16 = (256 * payload[2*i+18] + payload[2*i+19]) - 65535 if 256 * payload[2*i+18] + payload[2*i+19] > 32767  else  256 * payload[2*i+18] + payload[2*i+19]
                mags[i] = (2 * mag_int16) / math.pow(2,16) 

  
            temp_int16 = (256 * payload[2*i+24] + payload[2*i+25]) - 65535 if 256 * payload[2*i+24] + payload[2*i+25] > 32767  else  256 * payload[2*i+24] + payload[2*i+25]
            temp = (200 * temp_int16) / math.pow(2,16)
        
            # Counter Value
            itow = 16777216 * payload[26] + 65536 * payload[27] + 256 * payload[28] + payload[29]   

            # BIT Value
            bit = 256 * payload[30] + payload[31]         

            data = collections.OrderedDict([('rollAngle', angles[0]),('pitchAngle', angles[1]),('yawAngleMag', angles[2]), \
                    ('xRateCorrected' , gyros[0]), ('yRateCorrected' , gyros[1]), ('zRateCorrected', gyros[2]), \
                    ( 'xAccel', accels[0]), ('yAccel', accels[1]), ('zAccel', accels[2]), \
                    ( 'xMag', mags[0]), ('yMag', mags[1]), ('zMag', mags[2]), ('xRateTemp', temp), \
                    ('timeITOW', itow), ('BITstatus', bit )])


            if self.logging == 1 and self.logger is not None:
                self.logger.log(data, self.odr_setting) 
            
            return data

        elif self.packet_type == 'SF':
            n = payload[0]
            for i in range(n):
                print('Set Field: 0x{0:02X}'.format(payload[i*2+1]) + '{0:02X}'.format(payload[i*2+2]))
        elif self.packet_type == 'WF':
            n = payload[0]
            for i in range(n):
                print('Write Field: 0x{0:02X}'.format(payload[i*2+1]) + '{0:02X}'.format(payload[i*2+2])) 
        elif self.packet_type == 'RF':
            n = payload[0]
            for i in range(n):
                print(( 'Read Field: 0x{0:02X}'.format(payload[i*4+1]) + '{0:02X}'.format(payload[i*4+2]) 
                + ' set to: 0x{0:02X}{1:02X}'.format(payload[i*4+3],payload[i*4+4])  
                + ' ({0:1c}{1:1c})'.format(payload[i*4+3],payload[i*4+4]) ))
            return payload
        elif self.packet_type == 'GF':
            n = payload[0]
            for i in range(n):
                print(( 'Get Field: 0x{0:02X}'.format(payload[i*4+1]) + '{0:02X}'.format(payload[i*4+2]) 
                + ' set to: 0x{0:02X}{1:02X}'.format(payload[i*4+3],payload[i*4+4])  
                + ' ({0:1c}{1:1c})'.format(payload[i*4+3],payload[i*4+4]) ))
        elif self.packet_type == 'VR':
            '''this packet type is obsolete'''
            print('Version String: {0}.{1}.{2}.{3}.{4}'.format(*payload))
        elif self.packet_type == 'ID':
            sn = int(payload[0] << 24) + int(payload[1] << 16) + int(payload[2] << 8) + int(payload[3])
            print('ID String: {0} {1}'.format(sn,payload[4:]))
            return '{0} {1}'.format(sn,payload[4:])

    def calc_crc(self,payload):
        '''Calculates CRC per 380 manual
        '''
        crc = 0x1D0F
        for bytedata in payload:
           crc = crc^(bytedata << 8) 
           for i in range(0,8):
                if crc & 0x8000:
                    crc = (crc << 1)^0x1021
                else:
                    crc = crc << 1

        crc = crc & 0xffff
        return crc

if __name__ == "__main__":
    grab = GrabIMU380Data()
    grab.start_log()