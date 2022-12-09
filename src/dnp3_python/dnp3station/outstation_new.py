from __future__ import annotations

import logging
import sys

import pydnp3.asiopal
from pydnp3 import opendnp3, openpal, asiopal, asiodnp3
import time

from typing import Union, Type, Dict

from .station_utils import master_to_outstation_command_parser
from .station_utils import OutstationCmdType, MasterCmdType
# from .outstation_utils import MeasurementType
from .station_utils import DBHandler

LOG_LEVELS = opendnp3.levels.NORMAL | opendnp3.levels.ALL_COMMS
LOCAL_IP = "0.0.0.0"
PORT = 20000
# PORT = 20001

stdout_stream = logging.StreamHandler(sys.stdout)
stdout_stream.setFormatter(logging.Formatter('%(asctime)s\t%(name)s\t%(levelname)s\t%(message)s'))

_log = logging.getLogger(__name__)
_log.addHandler(stdout_stream)
_log.setLevel(logging.DEBUG)
# _log.setLevel(logging.ERROR)
_log.setLevel(logging.INFO)

# alias
PointValueType = Union[opendnp3.Analog, opendnp3.Binary, opendnp3.AnalogOutputStatus, opendnp3.BinaryOutputStatus]


class MyOutstationWrapper:
    def __init__(self,
                 masterstation_ip_str: str = "0.0.0.0",
                 outstation_ip_str: str = "127.0.0.1",
                 port: int = 20000,
                 masterstation_id_int: int = 2,
                 outstation_id_int: int = 1,
                 concurrencyHint: int = 1,

                 channel_log_level=opendnp3.levels.NORMAL,
                 outstation_log_level=opendnp3.levels.NORMAL,
                 ):
        self.outstation_appl = MyOutStationNew(
            masterstation_ip_str,
            outstation_ip_str,
            port,
            masterstation_id_int,
            outstation_id_int,
            concurrencyHint,

            channel_log_level,
            outstation_log_level,
        )


class MyOutStationNew(opendnp3.IOutstationApplication):
    """
            Interface for all outstation callback info except for control requests.

            DNP3 spec section 5.1.6.2:
                The Application Layer provides the following services for the DNP3 User Layer in an outstation:
                    - Notifies the DNP3 User Layer when action requests, such as control output,
                      analog output, freeze and file operations, arrive from a master.
                    - Requests data and information from the outstation that is wanted by a master
                      and formats the responses returned to a master.
                    - Assures that event data is successfully conveyed to a master (using
                      Application Layer confirmation).
                    - Sends notifications to the master when the outstation restarts, has queued events,
                      and requires time synchronization.

            DNP3 spec section 5.1.6.3:
                The Application Layer requires specific services from the layers beneath it.
                    - Partitioning of fragments into smaller portions for transport reliability.
                    - Knowledge of which device(s) were the source of received messages.
                    - Transmission of messages to specific devices or to all devices.
                    - Message integrity (i.e., error-free reception and transmission of messages).
                    - Knowledge of the time when messages arrive.
                    - Either precise times of transmission or the ability to set time values
                      into outgoing messages.
        """

    outstation = None
    db_handler = None
    outstation_application = None
    outstation_pool = {}  # a pool of outstations
    outstation_application_pool: Dict[str, MyOutStationNew] = {}  # a pool of outstation applications

    def __init__(self,
                 masterstation_ip_str: str = "0.0.0.0",
                 outstation_ip_str: str = "127.0.0.1",
                 port: int = 20000,
                 masterstation_id_int: int = 2,
                 outstation_id_int: int = 1,
                 concurrencyHint: int = 1,

                 channel_log_level=opendnp3.levels.NORMAL,
                 outstation_log_level=opendnp3.levels.NORMAL,
                 ):
        super().__init__()

        # TODO: refactor to apply factory pattern, allow further config
        # - the init parameter list is a bit long.
        # - allow configuration method after init

        self.masterstation_id_int = masterstation_id_int
        self.outstation_id_int = outstation_id_int

        _log.debug('Configuring the DNP3 stack.')
        _log.debug('Configuring the outstation database.')
        self.stack_config = self.configure_stack()  # TODO: refactor it to outside of the class

        # TODO: Justify if this is is really working? (Not sure if it really takes effect yet.)
        #  but needs to add docstring. Search for "intriguing" in "data_retrieval_demo.py"
        # Note: dbconfig signature at cpp/libs/include/asiodnp3/DatabaseConfig.h
        # which has sizes parameter
        self.configure_database(self.stack_config.dbConfig)  # TODO: refactor it to outside of the class.
        # print("=====verify self.stack_config.dbConfig.analog[0].svariation",
        #       self.stack_config.dbConfig.analog[0].svariation)
        # self.stack_config.dbConfig.analog[0].svariation = opendnp3.StaticAnalogVariation.Group30Var5
        # print("=====verify again self.stack_config.dbConfig.analog[0].svariation",
        #       self.stack_config.dbConfig.analog[0].svariation)
        #
        # print("====self.stack_config.dbConfig.analog[0].svariation type", type(self.stack_config.dbConfig.analog[0].svariation ))
        #
        # # print("=======experiment", self.stack_config.dbConfig.binary.toView)
        # print("=======after self.stack_config", self.stack_config.dbConfig)
        # print("=======experiment", self.stack_config.dbConfig.sizes.numAnalog)

        # threads_to_allocate = 1
        # self.log_handler = MyLogger()
        self.log_handler = asiodnp3.ConsoleLogger().Create()  # (or use this during regression testing)
        # self.manager = asiodnp3.DNP3Manager(threads_to_allocate, self.log_handler)
        # print("====outstation self.log_handler = log_handler", self.log_handler)

        # init steps: DNP3Manager(manager) -> TCPClient(channel) -> Master(master)
        # init DNP3Manager(manager)
        _log.debug('Creating a DNP3Manager.')
        self.manager = asiodnp3.DNP3Manager(concurrencyHint, self.log_handler)  # TODO: play with concurrencyHint

        # init TCPClient(channel)
        _log.debug('Creating the DNP3 channel, a TCP server.')
        self.retry_parameters = asiopal.ChannelRetry().Default()
        self.listener = AppChannelListener()
        # self.listener = asiodnp3.PrintingChannelListener().Create()       # (or use this during regression testing)
        level = opendnp3.levels.NORMAL | opendnp3.levels.ALL_COMMS  # seems not working
        self.channel = self.manager.AddTCPServer(id="server",
                                                 levels=level,
                                                 retry=self.retry_parameters,
                                                 endpoint=outstation_ip_str,
                                                 port=port,
                                                 listener=self.listener)

        _log.debug('Adding the outstation to the channel.')
        self.outstation_id = outstation_ip_str + "-" + str(port)
        self.command_handler = OutstationCommandHandler()
        self.command_handler = MyOutstationCommandHandler()
        # Note: use post init to link outstation application and OutstationCommandHandler instance(object)
        self.command_handler.post_init(outstation_id=self.outstation_id)
        # self.command_handler =  opendnp3.SuccessCommandHandler().Create() # (or use this during regression testing)
        # self.outstation_application = OutstationApplication()
        self.outstation_application = self
        # self.outstation_application = asiodnp3.Default
        MyOutStationNew.set_outstation_application(outstation_application=self)  # Note: don't touch this
        self.outstation = self.channel.AddOutstation(id="outstation-" + self.outstation_id,
                                                     commandHandler=self.command_handler,
                                                     # application=self,
                                                     # application=self.outstation_application,
                                                     application=MyOutStationNew.outstation_application,
                                                     config=self.stack_config)

        # MyOutStationNew.add_outstation(outstation_id=self.outstation_id, outstation=self.outstation)

        MyOutStationNew.add_outstation_app(outstation_id=self.outstation_id, outstation_app=self.outstation_application)

        # Configure log level for channel(tcpclient) and outstation
        # note: one of the following
        #   ALL = -1
        #   ALL_APP_COMMS = 129024
        #   ALL_COMMS = 130720
        #   NORMAL = 15
        #   NOTHING = 0

        _log.debug('Configuring log level')
        self.channel_log_level: opendnp3.levels = channel_log_level
        self.outstation_log_level: opendnp3.levels = outstation_log_level

        self.channel.SetLogFilters(openpal.LogFilters(self.channel_log_level))
        self.outstation.SetLogFilters(openpal.LogFilters(self.outstation_log_level))
        # self.channel.SetLogFilters(openpal.LogFilters(opendnp3.levels.ALL_COMMS))
        # self.master.SetLogFilters(openpal.LogFilters(opendnp3.levels.ALL_COMMS))

        # Put the Outstation singleton in OutstationApplication so that it can be used to send updates to the Master.
        # MyOutStationNew.set_outstation(self.outstation)  # Note: this needs to be self.outstation (not cls.outstation)
        #
        self.db_handler = DBHandler(stack_config=self.stack_config)
        # MyOutStationNew.set_db_handler(self.db_handler)

    @classmethod
    def add_outstation(cls, outstation_id: str, outstation: pydnp3.asiodnp3.IOutstation):
        """add outstation instance to outstation pool,
        the id is in the format of `ip-port`, e.g., `0.0.0.0-20000`."""
        cls.outstation_pool[outstation_id] = outstation

    @classmethod
    def get_outstation(cls, outstation_id: str) -> pydnp3.asiodnp3.IOutstation:
        """get outstation instance from the outstation pool using outstation id,
        the id is in the format of `ip-port`, e.g., `0.0.0.0-20000`."""
        return cls.outstation_pool.get(outstation_id)

    @classmethod
    def add_outstation_app(cls, outstation_id: str, outstation_app: MyOutStationNew):
        """add outstation instance to outstation pool,
        the id is in the format of `ip-port`, e.g., `0.0.0.0-20000`."""
        cls.outstation_application_pool[outstation_id] = outstation_app

    @classmethod
    def get_outstation_app(cls, outstation_id: str) -> MyOutStationNew:
        """get outstation instance from the outstation pool using outstation id,
        the id is in the format of `ip-port`, e.g., `0.0.0.0-20000`."""
        return cls.outstation_application_pool.get(outstation_id)

    @classmethod
    def set_outstation_application(cls, outstation_application):
        """
            use singleton
            Note: at this version,needs to keep this fuction
        """
        cls.outstation_application = outstation_application

    @classmethod
    def set_db_handler(cls, db_handler):
        """
            Set the singleton instance of IOutstation, as returned from the channel's AddOutstation call.

            Making IOutstation available as a singleton allows other classes (e.g. the command-line UI)
            to send commands to it -- see apply_update().
        """
        cls.db_handler = db_handler


    # @staticmethod
    def configure_stack(self):
        """Set up the OpenDNP3 configuration."""
        stack_config = asiodnp3.OutstationStackConfig(opendnp3.DatabaseSizes.AllTypes(10))
        # stack_config = asiodnp3.OutstationStackConfig(opendnp3.DatabaseSizes.Empty())
        # stack_config = asiodnp3.OutstationStackConfig(dbSizes=opendnp3.DatabaseSizes.AnalogOnly(8))
        # TODO: expose DatabaseSizes to public interface
        # stack_config = asiodnp3.OutstationStackConfig(dbSizes=opendnp3.DatabaseSizes(numBinary=10,
        #                                                                              numDoubleBinary=0,
        #                                                                              numAnalog=10,
        #                                                                              numCounter=0,
        #                                                                              numFrozenCounter=0,
        #                                                                              numBinaryOutputStatus=10,
        #                                                                              numAnalogOutputStatus=10,
        #                                                                              numTimeAndInterval=0))

        stack_config.outstation.eventBufferConfig = opendnp3.EventBufferConfig().AllTypes(10)
        stack_config.outstation.params.allowUnsolicited = True  # TODO: create interface for this
        stack_config.link.LocalAddr = self.outstation_id_int  # meaning for outstation, use 1 to follow simulator's default
        stack_config.link.RemoteAddr = self.masterstation_id_int  # meaning for master station, use 2 to follow simulator's default
        stack_config.link.KeepAliveTimeout = openpal.TimeDuration().Max()
        return stack_config

    @staticmethod
    def configure_database(db_config):
        """
            Configure the Outstation's database of input point definitions.

            # Configure two Analog points (group/variation 30.1) at indexes 1 and 2.
            Configure two Analog points (group/variation 30.1) at indexes 0, 1.
            Configure two Binary points (group/variation 1.2) at indexes 1 and 2.
        """
        # TODO: figure out the right way to configure

        # AnalogInput
        db_config.analog[0].clazz = opendnp3.PointClass.Class2
        # db_config.analog[0].svariation = opendnp3.StaticAnalogVariation.Group30Var1
        db_config.analog[
            0].svariation = opendnp3.StaticAnalogVariation.Group30Var5  # note: experiment, Analog input - double-precision, floating-point with flag ref: https://docs.stepfunc.io/dnp3/0.9.0/dotnet/namespacednp3.html#aa326dc3592a41ae60222051044fb084f
        db_config.analog[0].evariation = opendnp3.EventAnalogVariation.Group32Var7
        db_config.analog[1].clazz = opendnp3.PointClass.Class2
        db_config.analog[1].svariation = opendnp3.StaticAnalogVariation.Group30Var1
        db_config.analog[1].evariation = opendnp3.EventAnalogVariation.Group32Var7
        # db_config.analog[2].clazz = opendnp3.PointClass.Class2
        # db_config.analog[2].svariation = opendnp3.StaticAnalogVariation.Group30Var1
        # db_config.analog[2].evariation = opendnp3.EventAnalogVariation.Group32Var7

        # BinaryInput
        db_config.binary[0].clazz = opendnp3.PointClass.Class2
        db_config.binary[0].svariation = opendnp3.StaticBinaryVariation.Group1Var2
        db_config.binary[0].evariation = opendnp3.EventBinaryVariation.Group2Var2
        db_config.binary[1].clazz = opendnp3.PointClass.Class2
        db_config.binary[1].svariation = opendnp3.StaticBinaryVariation.Group1Var2
        db_config.binary[1].evariation = opendnp3.EventBinaryVariation.Group2Var2
        db_config.binary[2].clazz = opendnp3.PointClass.Class2
        db_config.binary[2].svariation = opendnp3.StaticBinaryVariation.Group1Var2
        db_config.binary[2].evariation = opendnp3.EventBinaryVariation.Group2Var2

        # Kefei's wild guess for analog output config
        db_config.aoStatus[0].clazz = opendnp3.PointClass.Class2
        db_config.aoStatus[0].svariation = opendnp3.StaticAnalogOutputStatusVariation.Group40Var1
        # db_config.aoStatus[0].evariation = opendnp3.StaticAnalogOutputStatusVariation.Group40Var1
        db_config.boStatus[0].clazz = opendnp3.PointClass.Class2
        db_config.boStatus[0].svariation = opendnp3.StaticBinaryOutputStatusVariation.Group10Var2
        # db_config.boStatus[0].evariation = opendnp3.StaticBinaryOutputStatusVariation.Group10Var2

    def start(self):
        _log.debug('Enabling the outstation.')
        self.outstation.Enable()

    def shutdown(self):
        """
        Execute an orderly shutdown of the Outstation.
        The debug messages may be helpful if errors occur during shutdown.

        Expected:
            ms(1667102887814) INFO    server - Operation aborted.
            ms(1667102887821) INFO    manager - Exiting thread (0)

        Note:
            Note: Don't use `self.manager.Shutdown()`, otherwise
            Process finished with exit code 134 (interrupted by signal 6: SIGABRT)
        """
        time.sleep(2)  # Note: hard-coded sleep to avoid hanging process
        # _outstation = self.get_outstation()
        _outstation = self.outstation
        _outstation.Shutdown()
        # del _outstation
        self.channel.Shutdown()

        # self.manager.Shutdown()

    @classmethod  # Note: Using classmethod is required
    def get_outstation(cls):
        """Get the singleton instance of IOutstation."""
        return cls.outstation

    @classmethod
    def set_outstation(cls, outstn):
        """
            Set the singleton instance of IOutstation, as returned from the channel's AddOutstation call.

            Making IOutstation available as a singleton allows other classes (e.g. the command-line UI)
            to send commands to it -- see apply_update().
        """
        cls.outstation = outstn

    # @classmethod
    def process_point_value(self, command_type, command, index, op_type):
        """
            A PointValue was received from the Master. Process its payload then up database (For control workflow)
            Note: parse master operation command to outstation update command, then reuse apply_update.

        :param command_type: (string) Either 'Select' or 'Operate'.
        :param command: One of ControlRelayOutputBlock, AnalogOutputDouble64, AnalogOutputFloat32, AnalogOutputInt32,
                        or AnalogOutputInt16.
        :param index: (integer) DNP3 index of the payload's data definition.
        :param op_type: An OperateType, or None if command_type == 'Select'.
        """
        # TODO: add control logic in scenarios 'Select' or 'Operate' (to allow more sophisticated control behavior)

        # print("command __getattribute__ ", command.__getattribute__)
        _log.debug('Processing received point value for index {}: {}'.format(index, command))

        # parse master operation command to outstation update command
        # Note: print("command rawCode ", command.rawCode) for BinaryOutput/ControlRelayOutputBlock
        # Note: print("command value ", command.value)  for AnalogOutput/AnalogOutputDouble64, etc.
        outstation_cmd = master_to_outstation_command_parser(command)
        # then reuse apply_update
        # cls.apply_update(outstation_cmd, index)
        self.apply_update(outstation_cmd, index)

    # @classmethod
    def apply_update(self,
                     measurement: OutstationCmdType,
                     index):
        """
            Record an opendnp3 data value (Analog, Binary, etc.) in the outstation's database.
            Note: measurement based on asiodnp3.UpdateBuilder.Update(**args)

            The data value gets sent to the Master as a side effect.

        :param measurement: An instance of Analog, Binary, or another opendnp3 data value.
        :param index: (integer) Index of the data definition in the opendnp3 database.
        """
        _log.debug('Recording {} measurement, index={}, '
                   'value={}, flag={}, time={}'
                   .format(type(measurement), index, measurement.value, measurement.flags.value, measurement.time.value))
        # builder = asiodnp3.UpdateBuilder()
        # builder.Update(measurement, index)
        # update = builder.Build()
        update = asiodnp3.UpdateBuilder().Update(measurement, index).Build()
        # cls.get_outstation().Apply(update)
        self.outstation.Apply(update)

        # cls.db_handler.process(measurement, index)
        self.db_handler.process(measurement, index)

    def __del__(self):
        try:
            self.shutdown()
        except Exception:
            pass


class MyOutstationCommandHandler(opendnp3.ICommandHandler):
    """
        Override ICommandHandler in this manner to implement application-specific command handling.

        ICommandHandler implements the Outstation's handling of Select and Operate,
        which relay commands and data from the Master to the Outstation.

        Note: this class CANNOT implement init
    """

    # outstation_application = MyOutStationNew
    outstation_id = ""

    # def __init__(self, outstation_id="some-id"):
    #     self.outstation_id = outstation_id

    def post_init(self, outstation_id, **kwargs):
        """helper function to pass values, e.g., outstation_id"""
        self.outstation_id = outstation_id

    def Start(self):
        _log.debug('In OutstationCommandHandler.Start')

    def End(self):
        _log.debug('In OutstationCommandHandler.End')

    def Select(self, command, index):
        """
            The Master sent a Select command to the Outstation. Handle it.
            Note: master SelectAndOperate use this method

        :param command: ControlRelayOutputBlock,
                        AnalogOutputInt16, AnalogOutputInt32, AnalogOutputFloat32, or AnalogOutputDouble64.
        :param index: int
        :return: CommandStatus
        """
        outstation_application_pool = MyOutStationNew.outstation_application_pool
        outstation_app = outstation_application_pool.get(self.outstation_id)

        try:
            outstation_app.process_point_value('Select', command, index, None)
            return opendnp3.CommandStatus.SUCCESS
        except Exception as e:
            _log.error(e)
            raise e

    def Operate(self, command, index, op_type):
        """
            The Master sent an Operate command to the Outstation. Handle it.
            Note: master SelectAndOperate use this method
            Note: master DirectOperate use this method

        :param command: ControlRelayOutputBlock,
                        AnalogOutputInt16, AnalogOutputInt32, AnalogOutputFloat32, or AnalogOutputDouble64.
        :param index: int
        :param op_type: OperateType
        :return: CommandStatus
        """

        outstation_application_pool = MyOutStationNew.outstation_application_pool
        outstation_app = outstation_application_pool.get(self.outstation_id)
        try:
            # self.outstation_application.process_point_value('Operate', command, index, op_type)
            outstation_app.process_point_value('Operate', command, index, op_type)
            return opendnp3.CommandStatus.SUCCESS
        except Exception as e:
            _log.error(e)
            raise e


class AppChannelListener(asiodnp3.IChannelListener):
    """
        Override IChannelListener in this manner to implement application-specific channel behavior.
    """

    def __init__(self):
        super(AppChannelListener, self).__init__()

    def OnStateChange(self, state):
        _log.debug('In AppChannelListener.OnStateChange: state={}'.format(state))


class OutstationCommandHandler(opendnp3.ICommandHandler):
    """
        Override ICommandHandler in this manner to implement application-specific command handling.
        ICommandHandler implements the Outstation's handling of Select and Operate,
        which relay commands and data from the Master to the Outstation.
    """

    outstation_application = MyOutStationNew

    def Start(self):
        _log.debug('In OutstationCommandHandler.Start')

    def End(self):
        _log.debug('In OutstationCommandHandler.End')

    def Select(self, command, index):
        """
            The Master sent a Select command to the Outstation. Handle it.
            Note: master SelectAndOperate use this method
        :param command: ControlRelayOutputBlock,
                        AnalogOutputInt16, AnalogOutputInt32, AnalogOutputFloat32, or AnalogOutputDouble64.
        :param index: int
        :return: CommandStatus
        """
        # print("===========command, ", command)
        # print("===========index, ", index)
        self.outstation_application.process_point_value('Select', command, index, None)
        return opendnp3.CommandStatus.SUCCESS

    def Operate(self, command, index, op_type):
        """
            The Master sent an Operate command to the Outstation. Handle it.
            Note: master SelectAndOperate use this method
            Note: master DirectOperate use this method
        :param command: ControlRelayOutputBlock,
                        AnalogOutputInt16, AnalogOutputInt32, AnalogOutputFloat32, or AnalogOutputDouble64.
        :param index: int
        :param op_type: OperateType
        :return: CommandStatus
        """
        # print("Operate===========command, ", command)
        # print("Operate===========index, ", index)
        # print("Operate===========op_type, ", op_type)
        self.outstation_application.process_point_value('Operate', command, index, op_type)
        return opendnp3.CommandStatus.SUCCESS
