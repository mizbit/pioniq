#!/usr/bin/python

import paho.mqtt.publish as publish
import paho.mqtt.client as mqtt
import ssl
import time
import json
import logging
import logging.handlers
import os

import obd
from obd import OBDCommand, OBDStatus
from obd.protocols import ECU
from obd.decoders import raw_string
from obd.utils import bytes_to_int

class ConnectionError(Exception): pass

class CanError(Exception): pass

# CAN response decoder. This function returns a bytearray containing ONLY the data.
# CAN response data format:

# Single frame
# [0-3] Identifier
# [3-4] Frame type
#       Frame type: 0 = single frame
# [4-5] Data length
# [5-19] Data. Keep in mind that data length may be shorter than the length of the array, so you should read up to data length.

# First frame (multiple frames)
# [0-3] Identifier
# [3-4] Frame type
#       Frame type: 1 = First frame (multiple frames)
# [4-7] Data length
# [7-19] Data

# Consecutive frame
# [0-3] Identifier
# [3-4] Frame type
#       Frame type: 2 = Consecutive frame
# [4-19] Data. Keep in mind that for last frame data length may be shorter than the length of the array, so you should read up to data length.

# For example:
# Having the following CAN response frames:
# 7EC103D6101FFFFFFFF
# 7EC21A9264826480300
# 7EC22050EFA1F1F1F1F
# 7EC231F1F1F001DC714
# 7EC24C70A012A910001
# 7EC25547A000151B300
# 7EC26007AD100007718
# 7EC27005928B40D017F
# 7EC280000000003E800

# It will be decomposed as:
# 7EC 1 03D 6101FFFFFFFF
# 7EC 2 1 A9264826480300
# 7EC 2 2 050EFA1F1F1F1F
# 7EC 2 3 1F1F1F001DC714
# 7EC 2 4 C70A012A910001
# 7EC 2 5 547A000151B300
# 7EC 2 6 007AD100007718
# 7EC 2 7 005928B40D017F
# 7EC 2 8 0000000003E8 00

# First frame:
# Identifier  Frame type                        Data length (03D = 61 bytes) Data
#  |          |                                 |                            |
# 7EC         1                                03D                           6101FFFFFFFF -> 6 bytes of data
# 
# Consecutive frames:
# Identifier  Frame type                        Line index  Data
#  |          |                                 |           |
# 7EC         2                                 1           A9264826480300 -> +7 bytes of data (total 13 bytes)
# 7EC         2                                 2           050EFA1F1F1F1F -> +7 bytes of data (total 20 bytes)
# 7EC         2                                 3           1F1F1F001DC714 -> +7 bytes of data (total 27 bytes)
# 7EC         2                                 4           C70A012A910001 -> +7 bytes of data (total 34 bytes)
# 7EC         2                                 5           547A000151B300 -> +7 bytes of data (total 41 bytes)
# 7EC         2                                 6           007AD100007718 -> +7 bytes of data (total 48 bytes)
# 7EC         2                                 7           005928B40D017F -> +7 bytes of data (total 55 bytes)
# 7EC         2                                 8           0000000003E8  00 -> + 6 bytes of data (total 61 bytes)
#                                                                         |
#                                                                         Not part of the data (as it's bigger than 03D = 61 bytes of data)
def can_response(can_message):
    data = None
    data_len = 0
    last_idx = 0
    raw = can_message[0].raw().split('\n')
    for line in raw:
        if (len(line) != 19):
            raise ValueError('Error parsing CAN response: {}. Invalid line length {}!=19. '.format(line,len(line)))

        offset = 3
        identifier = int(line[0:offset], 16)

        frame_type = int(line[offset:offset+1], 16)

        if frame_type == 0:     # Single frame
            data_len = int(line[offset+1:offset+2], 16)
            data = bytes.fromhex(line[offset+2:data_len*2+offset+2])
            break

        elif frame_type == 1:   # First frame
            data_len = int(line[offset+1:offset+4], 16)
            data = bytearray.fromhex(line[offset+4:])
            last_idx = 0

        elif frame_type == 2:   # Consecutive frame
            idx = int(line[offset+1:offset+2], 16)
            if (last_idx + 1) % 0x10 != idx:
                raise CanError("Bad frame order: last_idx({}) idx({})".format(last_idx,idx))

            frame_len = min(7, data_len - len(data))
            data.extend(bytearray.fromhex(line[offset+2:frame_len*2+offset+2]))
            last_idx = idx

            if data_len == len(data):
                break

        else:                   # Unexpected frame
            raise ValueError('Unexpected frame')
    return data

# The same as can_response decoder but logging data in binary, decimal and hex for debugging purposes
def log_can_response(can_message):
    raw = can_response(can_message)
    for i in range(0, len(raw)):
        logger.debug("Data[{}]:{} - {} - {}".format(i,'{0:08b}'.format(raw[i]),raw[i], hex(raw[i])))
    return raw

# Extract VIN from raw can response
def extract_vin(raw_can_response): 
    vin_str = ""
    for v in range(16, 33):
        vin_str = vin_str + chr(bytes_to_int(raw_can_response.value[v:v+1]))
    return vin_str

# Extract gear stick position from raw can response
def extract_gear(raw_can_response): 
    gear_str = ""
    gear_bits = raw_can_response.value[7]
    logger.debug("Gear:{} - {} - {}".format('{0:08b}'.format(gear_bits),gear_bits, hex(gear_bits)))
    if gear_bits & 0x1: # 1st bit is 1
        gear_str = gear_str + "P" 
    if gear_bits & 0x2: # 2nd bit is 1
        gear_str = gear_str + "R"
    if gear_bits & 0x4: # 3rd bit is 1
        gear_str = gear_str + "N"
    if gear_bits & 0x8: # 4th bit is 1
        gear_str = gear_str + "D"
    if gear_bits & 0x10: # 5th bit is 1
        gear_str = gear_str + "B"

    return gear_str

def obd_connect():
    connection_count = 0
    obd_connection = None
    while (obd_connection is None or obd_connection.status() != OBDStatus.CAR_CONNECTED) and connection_count < MAX_ATTEMPTS:
        connection_count += 1
        # Establish connection with OBDII dongle
        obd_connection = obd.OBD(portstr=config['serial']['port'], baudrate=int(config['serial']['baudrate']), fast=False, timeout=30)
        if (obd_connection is None or obd_connection.status() != OBDStatus.CAR_CONNECTED) and connection_count < MAX_ATTEMPTS:
            logger.warning("{}. Retrying in {} second(s)...".format(obd_connection.status(), connection_count))
            time.sleep(connection_count)

    if obd_connection.status() != OBDStatus.CAR_CONNECTED:
        raise ConnectionError(obd_connection.status())
    else:
        return obd_connection

def query_command(command):
    command_count = 0
    cmd_response = None
    exception = False
    valid_response = False
    while not valid_response and command_count < MAX_ATTEMPTS:
        command_count += 1
        try:
            cmd_response = connection.query(command)
        except Exception as ex:
            exception = True
        valid_response = not(cmd_response is None or cmd_response.value == "?" or cmd_response.value == "NO DATA" or cmd_response.value == "" or cmd_response.value is None or exception)
        if not valid_response and command_count < MAX_ATTEMPTS:
            logger.warning("No valid response for {}. Retrying in {} second(s)...".format(command, command_count))
            time.sleep(command_count)

    if not valid_response:
        raise ValueError("No valid response for {}. Max attempts ({}) exceeded.".format(command, MAX_ATTEMPTS))
    else:
        logger.debug("{} got response".format(command))
        return cmd_response

def query_battery_information():
    logger.info("**** Querying battery information ****")
    # Set header to 7E4.
    query_command(cmd_can_header_7e4)
    # Set the CAN receive address to 7EC
    query_command(cmd_can_receive_address_7ec)

    # 2101 - 2105 codes to get battery status information
    raw_2101 = query_command(cmd_bms_2101)
    raw_2102 = query_command(cmd_bms_2102)
    raw_2103 = query_command(cmd_bms_2103)
    raw_2104 = query_command(cmd_bms_2104)
    raw_2105 = query_command(cmd_bms_2105)
    
    # Extract status of health value from responses
    soh = bytes_to_int(raw_2105.value[27:29]) / 10.0

    battery_info = {}
    # Only create battery status data if got a consistent Status Of Health (sometimes it's not consistent)
    if (soh <= 100):
        chargingBits = raw_2101.value[11]
        dcBatteryCurrent = bytes_to_int(raw_2101.value[12:14]) / 10.0
        dcBatteryVoltage = bytes_to_int(raw_2101.value[14:16]) / 10.0
        
        cellTemps = [
            bytes_to_int(raw_2101.value[18:19]), #  0
            bytes_to_int(raw_2101.value[19:20]), #  1
            bytes_to_int(raw_2101.value[20:21]), #  2
            bytes_to_int(raw_2101.value[21:22]), #  3
            bytes_to_int(raw_2101.value[22:23]), #  4
            bytes_to_int(raw_2105.value[11:12]), #  5
            bytes_to_int(raw_2105.value[12:13]), #  6
            bytes_to_int(raw_2105.value[13:14]), #  7
            bytes_to_int(raw_2105.value[14:15]), #  8
            bytes_to_int(raw_2105.value[15:16]), #  9
            bytes_to_int(raw_2105.value[16:17]), # 10
            bytes_to_int(raw_2105.value[17:18])] # 11

        cellVoltages = []
        for cmd in [raw_2102, raw_2103, raw_2104]:
            for byte in range(6,38):
                cellVoltages.append(cmd.value[byte] / 50.0)

        battery_info.update({
            'timestamp':                       int(round(time.time())),

            'socBms':                          raw_2101.value[6] / 2.0, # %
            'socDisplay':                      int(raw_2105.value[33] / 2.0), # %
            'soh':                             soh, # %

            'bmsIgnition':                     1 if raw_2101.value[52] & 0x4 else 0, # 3rd bit is 1 
            'bmsMainRelay':                    1 if chargingBits & 0x1 else 0, # 1st bit is 1 
            'auxBatteryVoltage':               raw_2101.value[31] / 10.0, # V

            'charging':                        1 if chargingBits & 0x80 else 0, # 8th bit is 1
            'normalChargePort':                1 if chargingBits & 0x20 else 0, # 6th bit is 1
            'rapidChargePort':                 1 if chargingBits & 0x40 else 0, # 7th bit is 1

            'fanStatus':                       raw_2101.value[29], # Hz
            'fanFeedback':                     raw_2101.value[30],

            'cumulativeEnergyCharged':         bytes_to_int(raw_2101.value[40:44]) / 10.0, # kWh
            'cumulativeEnergyDischarged':      bytes_to_int(raw_2101.value[44:48]) / 10.0, # kWh

            'cumulativeChargeCurrent':         bytes_to_int(raw_2101.value[32:36]) / 10.0, # A
            'cumulativeDischargeCurrent':      bytes_to_int(raw_2101.value[36:40]) / 10.0, # A

            'cumulativeOperatingTime':         bytes_to_int(raw_2101.value[48:52]), # seconds 

            'availableChargePower':            bytes_to_int(raw_2101.value[7:9]) / 100.0, # kW
            'availableDischargePower':         bytes_to_int(raw_2101.value[9:11]) / 100.0, # kW

            'dcBatteryCellVoltageDeviation':   raw_2105.value[22] / 50, # V 
            'dcBatteryHeater1Temperature':     float(raw_2105.value[25]), # C 
            'dcBatteryHeater2Temperature':     float(raw_2105.value[26]), # C 
            'dcBatteryInletTemperature':       bytes_to_int(raw_2101.value[22:23]), # C
            'dcBatteryMaxTemperature':         bytes_to_int(raw_2101.value[16:17]), # C
            'dcBatteryMinTemperature':         bytes_to_int(raw_2101.value[17:18]), # C
            'dcBatteryCellNoMaxDeterioration': int(raw_2105.value[29]),
            'dcBatteryCellMinDeterioration':   bytes_to_int(raw_2105.value[30:32]) / 10.0, # %
            'dcBatteryCellNoMinDeterioration': int(raw_2105.value[32]),
            'dcBatteryCurrent':                dcBatteryCurrent, # A
            'dcBatteryPower':                  dcBatteryCurrent * dcBatteryVoltage / 1000.0, # kW
            'dcBatteryVoltage':                dcBatteryVoltage, # V
            'dcBatteryAvgTemperature':         sum(cellTemps) / len(cellTemps), # C

            'driveMotorSpeed':                 bytes_to_int(raw_2101.value[55:57]) # RPM
            })
    
        for i,temp in enumerate(cellTemps):
            key = "dcBatteryModuleTemp{:02d}".format(i+1)
            battery_info[key] = float(temp)

        for i,cvolt in enumerate(cellVoltages):
            key = "dcBatteryCellVoltage{:02d}".format(i+1)
            battery_info[key] = float(cvolt)

        logger.info("**** Got battery information ****")
    else:
        raise ValueError("Got inconsistent data for battery Status Of Health: {}%".format(soh))
    return battery_info


def query_odometer():
    logger.info("**** Querying for odometer ****")
    odometer_info = {}
    # Set header to 7C6
    query_command(cmd_can_header_7c6)
    # Set the CAN receive address to 7EC
    query_command(cmd_can_receive_address_7ec)
    # Sets the ID filter to 7CE
    query_command(cmd_can_filter_7ce)
    # Query odometer
    raw_odometer = query_command(cmd_odometer)
    # Only set odometer data if present. Not available when car engine is off
    if 'raw_odometer' in locals() and raw_odometer is not None and raw_odometer.value is not None:
        odometer_info.update({
            'timestamp': int(round(time.time())),
            'odometer': bytes_to_int(raw_odometer.value[9:12])
        })
        logger.info("**** Got odometer value ****")
    else:
        raise ValueError("Could not get odometer value")
    return odometer_info


def query_vmcu_information():
    logger.info("**** Querying for VMCU information ****")
    vmcu_info = {
        'timestamp': int(round(time.time()))
    }
    # Set header to 7E2
    query_command(cmd_can_header_7e2)
    # Set the CAN receive address to 7EA
    query_command(cmd_can_receive_address_7ea)

    raw_vin = query_command(cmd_vin)
    raw_2101 = query_command(cmd_vmcu_2101)
    
    vin = extract_vin(raw_vin)
    gear = extract_gear(raw_2101)
    mph_speed = (((raw_2101.value[16] * 256) + raw_2101.value[15]) / 100.0 )
    kmh_speed = mph_speed * 1.60934

    # Add vin to vmcu info
    if 'vin' in locals() and vin is not None :
        logger.info("**** Got Vehicle Identification Number ****")
        vmcu_info['vin'] = vin
    # Add gear stick position to vmcu info
    if 'gear' in locals() and gear is not None:
        logger.info("**** Got gear stick position ****")
        vmcu_info['gear'] = gear
    # Add kmh to vmcu info
    if 'kmh_speed' in locals() and kmh_speed is not None:
        logger.info("**** Got kmh speed ****")
        vmcu_info['kmh'] = kmh_speed

    return vmcu_info

def query_tpms_information():
    logger.info("**** Querying for TPMS information ****")
    tpms_info = {}
    # Set the CAN receive address to 7A8
    query_command(cmd_can_receive_address_7a8)
    # Set header to 7A0
    query_command(cmd_can_header_7a0)
    # Query TPMS
    raw_tpms = query_command(cmd_tpms_22c00b)
    if 'raw_tpms' in locals() and raw_tpms is not None and raw_tpms.value is not None:
        tpms_info.update({
            'timestamp': int(round(time.time())),
            'tire_1_pressure':    raw_tpms.value[7] * 0.2, # psi
            'tire_1_temperature': raw_tpms.value[8] - 55, # C
            
            'tire_2_pressure':    raw_tpms.value[11] * 0.2, # psi
            'tire_2_temperature': raw_tpms.value[12] - 55, # C
            
            'tire_3_pressure':    raw_tpms.value[15] * 0.2, # psi
            'tire_3_temperature': raw_tpms.value[16] - 55, # C
            
            'tire_4_pressure':    raw_tpms.value[19] * 0.2, # psi
            'tire_4_temperature': raw_tpms.value[20] - 55, # C
            })
        logger.info("**** Got tpms information ****")
    else:
        raise ValueError("Could not get TPMS information")
    return tpms_info

def query_external_temperature():
    logger.info("**** Querying for external temperature ****")
    ext_temp_info = {
        'timestamp': int(round(time.time()))
    }

    # Set header to 7E6
    query_command(cmd_can_header_7e6)
    # Set the CAN receive address to 7EC
    query_command(cmd_can_receive_address_7ee)
    # Query external temeprature
    ext_temp = query_command(cmd_ext_temp)
    # Only set odometer data if present. Not available when car engine is off
    if 'ext_temp' in locals() and ext_temp is not None and ext_temp.value is not None:
        logger.info("**** Got external temperature value ****")
        ext_temp_info['external_temperature'] = (ext_temp.value[14]-80) / 2.0 # C
    else:
        raise ValueError("Could not get external temperature value")
    return ext_temp_info

# Publish all messages to MQTT
def publish_data_mqtt(msgs):
    try:
        logger.info("Publish messages to MQTT")
        for msg in msgs:
            logger.info("{}".format(msg))

        publish.multiple(msgs,
                    hostname=broker_address,
                    port=port,
                    client_id="battery-data-script",
                    keepalive=60,
                    will=None,
                    auth={'username':user, 'password':password},
                    tls={'tls_version':ssl.PROTOCOL_TLS},
                    protocol=mqtt.MQTTv311,
                    transport="tcp")
        logger.info("{} message(s) published to MQTT".format(len(msgs)))
    except Exception as err:
        logger.error("Error publishing to MQTT: {}".format(err), exc_info=False)

# main script
if __name__ == '__main__':
    logger = logging.getLogger('battery')
    
    console_handler = logging.StreamHandler() # sends output to stderr
    console_handler.setFormatter(logging.Formatter("%(asctime)s %(name)-10s %(levelname)-8s %(message)s"))
    console_handler.setLevel(logging.DEBUG)
    logger.addHandler(console_handler)
    
    file_handler = logging.handlers.TimedRotatingFileHandler(os.path.dirname(os.path.realpath(__file__)) + '/battery_data.log',
                                                    when='midnight',
                                                    backupCount=15) # sends output to battery_data.log file rotating it at midnight and storing latest 15 days
    file_handler.setFormatter(logging.Formatter("%(asctime)s %(name)-10s %(levelname)-8s %(message)s"))
    file_handler.setLevel(logging.INFO)
    logger.addHandler(file_handler)

    logger.setLevel(logging.DEBUG)
    
    with open(os.path.dirname(os.path.realpath(__file__)) + '/battery_data.config.json') as config_file:
        config = json.loads(config_file.read())
    
    broker_address = config['mqtt']['broker']
    port = int(config['mqtt']['port'])
    user = config['mqtt']['user']
    password = config['mqtt']['password']
    topic_prefix = config['mqtt']['topic_prefix']
    
    mqtt_msgs = []
    
    MAX_ATTEMPTS = 3
    
    try:
        logger.info("=== Script start ===")
        
        # Add state data to messages array
        mqtt_msgs.extend([{'topic':topic_prefix + "state", 'payload':"ON", 'qos':0, 'retain':True}])
        
        obd.logger.setLevel(obd.logging.DEBUG)
        # Remove obd logger existing handlers
        for handler in obd.logger.handlers[:]:
            obd.logger.removeHandler(handler)
         # Add handlers to obd logger
        obd.logger.addHandler(console_handler)
        obd.logger.addHandler(file_handler)
    
        connection = obd_connect()

        cmd_can_header_7e4 =  OBDCommand("ATSH7E4",
                                "Set CAN module ID to 7E4 - BMS battery information",
                                b"ATSH7E4",
                                0,
                                raw_string,
                                ECU.ALL,
                                False)

        cmd_can_header_7c6 =  OBDCommand("ATSH7C6",
                                "Set CAN module ID to 7C6 - Odometer information",
                                b"ATSH7C6",
                                0,
                                raw_string,
                                ECU.ALL,
                                False)

        cmd_can_header_7e2 =  OBDCommand("ATSH7E2",
                                "Set CAN module ID to 7E2 - VMCU information",
                                b"ATSH7E2",
                                0,
                                raw_string,
                                ECU.ALL,
                                False)

        cmd_can_header_7a0 =  OBDCommand("ATSH7A0",
                                "Set CAN module ID to 7A0 - TPMS information",
                                b"ATSH7A0",
                                0,
                                raw_string,
                                ECU.ALL,
                                False)

        cmd_can_header_7e6 =  OBDCommand("ATSH7E6",
                                "Set CAN module ID to 7E6 - External temp information",
                                b"ATSH7E6",
                                0,
                                raw_string,
                                ECU.ALL,
                                False)

        cmd_can_receive_address_7ec = OBDCommand("ATCRA7EC",
                                            "Set the CAN receive address to 7EC",
                                            b"ATCRA7EC",
                                            0,
                                            raw_string,
                                            ECU.ALL,
                                            False)

        cmd_can_receive_address_7ea = OBDCommand("ATCRA7EA",
                                            "Set the CAN receive address to 7EA",
                                            b"ATCRA7EA",
                                            0,
                                            raw_string,
                                            ECU.ALL,
                                            False)

        cmd_can_receive_address_7a8 = OBDCommand("ATCRA7A8",
                                            "Set the CAN receive address to 7A8",
                                            b"ATCRA7A8",
                                            0,
                                            raw_string,
                                            ECU.ALL,
                                            False)

        cmd_can_receive_address_7ee = OBDCommand("ATCRA7EE",
                                            "Set the CAN receive address to 7EE",
                                            b"ATCRA7EE",
                                            0,
                                            raw_string,
                                            ECU.ALL,
                                            False)

        cmd_can_filter_7ce = OBDCommand("ATCF7CE",
                                    "Set the CAN filter to 7CE",
                                    b"ATCF7CE",
                                    0,
                                    raw_string,
                                    ECU.ALL,
                                    False)

        cmd_bms_2101 = OBDCommand("2101",
                            "Extended command - BMS Battery information",
                            b"2101",
                            0,
                            can_response,
                            ECU.ALL,
                            False)
    
        cmd_bms_2102 = OBDCommand("2102",
                            "Extended command - BMS Battery information",
                            b"2102",
                            0,
                            can_response,
                            ECU.ALL,
                            False)
    
        cmd_bms_2103 = OBDCommand("2103",
                            "Extended command - BMS Battery information",
                            b"2103",
                            0,
                            can_response,
                            ECU.ALL,
                            False)
    
        cmd_bms_2104 = OBDCommand("2104",
                            "Extended command - BMS Battery information",
                            b"2104",
                            0,
                            can_response,
                            ECU.ALL,
                            False)
    
        cmd_bms_2105 = OBDCommand("2105",
                            "Extended command - BMS Battery information",
                            b"2105",
                            0,
                            can_response,
                            ECU.ALL,
                            False)
    
        cmd_odometer = OBDCommand("ODOMETER",
                            "Extended command - Odometer information",
                            b"22b002",
                            0,
                            can_response,
                            ECU.ALL,
                            False)

        cmd_vin = OBDCommand("VIN",
                            "Extended command - Vehicle Identification Number",
                            b"1A80",
                            0,
                            can_response,
                            ECU.ALL,
                            False)

        cmd_vmcu_2101 = OBDCommand("2101",
                            "Extended command - VMCU information",
                            b"2101",
                            0,
                            can_response,
                            ECU.ALL,
                            False)

        cmd_tpms_22c00b = OBDCommand("22C00B",
                            "Extended command - TPMS information",
                            b"22C00B",
                            0,
                            can_response,
                            ECU.ALL,
                            False)

        cmd_ext_temp = OBDCommand("2180",
                            "Extended command - External temperature",
                            b"2180",
                            0,
                            can_response,
                            ECU.ALL,
                            False)

        # Add defined commands to supported commands
        connection.supported_commands.add(cmd_can_header_7e4)
        connection.supported_commands.add(cmd_can_header_7c6)
        connection.supported_commands.add(cmd_can_header_7e2)
        connection.supported_commands.add(cmd_can_header_7a0)
        connection.supported_commands.add(cmd_can_header_7e6)
        connection.supported_commands.add(cmd_can_receive_address_7ec)
        connection.supported_commands.add(cmd_can_receive_address_7ea)
        connection.supported_commands.add(cmd_can_receive_address_7a8)
        connection.supported_commands.add(cmd_can_receive_address_7ee)
        connection.supported_commands.add(cmd_can_filter_7ce)
        connection.supported_commands.add(cmd_bms_2101)
        connection.supported_commands.add(cmd_bms_2102)
        connection.supported_commands.add(cmd_bms_2103)
        connection.supported_commands.add(cmd_bms_2104)
        connection.supported_commands.add(cmd_bms_2105)
        connection.supported_commands.add(cmd_odometer)
        connection.supported_commands.add(cmd_vin)
        connection.supported_commands.add(cmd_vmcu_2101)
        connection.supported_commands.add(cmd_tpms_22c00b)
        connection.supported_commands.add(cmd_ext_temp)
    
        # Print supported commands
        # DTC = Diagnostic Trouble Codes
        # MIL = Malfunction Indicator Lamp
        logger.debug(connection.print_commands())
        
        try:
            # Add battery information to MQTT messages array
            mqtt_msgs.extend([{'topic':topic_prefix + "battery", 'payload':json.dumps(query_battery_information()), 'qos':0, 'retain':True}])
        except (ValueError, CanError) as err:
            logger.warning("**** Error querying battery information: {} ****".format(err), exc_info=False)

        try:
            # Add VMCU information to MQTT messages array
            mqtt_msgs.extend([{'topic':topic_prefix + "vmcu", 'payload':json.dumps(query_vmcu_information()), 'qos':0, 'retain':True}])
        except (ValueError, CanError) as err:
            logger.warning("**** Error querying vmcu information: {} ****".format(err), exc_info=False)

        try:
            # Add Odometer to MQTT messages array
            mqtt_msgs.extend([{'topic':topic_prefix + "odometer", 'payload':json.dumps(query_odometer()), 'qos':0, 'retain':True}])
        except (ValueError, CanError) as err:
            logger.warning("**** Error querying odometer: {} ****".format(err), exc_info=False)

        try:
            # Add TPMS information to MQTT messages array
            mqtt_msgs.extend([{'topic':topic_prefix + "tpms", 'payload':json.dumps(query_tpms_information()), 'qos':0, 'retain':True}])
        except (ValueError, CanError) as err:
            logger.warning("**** Error querying tpms information: {} ****".format(err), exc_info=False)

        try:
            # Add external temperture information to MQTT messages array
            mqtt_msgs.extend([{'topic':topic_prefix + "ext_temp", 'payload':json.dumps(query_external_temperature()), 'qos':0, 'retain':True}])
        except (ValueError, CanError) as err:
            logger.warning("**** Error querying tpms information: {} ****".format(err), exc_info=False)

    except ConnectionError as err:
        logger.error("OBDII connection error: {0}".format(err), exc_info=False)
    except ValueError as err:
        logger.error("Error found: {0}".format(err), exc_info=False)
    except CanError as err:
        logger.error("Error found reading CAN response: {0}".format(err), exc_info=False)
    except Exception as ex:
        logger.error("Unexpected error: {}".format(ex), exc_info=False)
    finally:
        publish_data_mqtt(mqtt_msgs)
        if 'connection' in locals() and connection is not None:
            connection.close()
        logger.info("===  Script end  ===")