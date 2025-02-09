import copy
import dataclasses as dc
import logging
import re
import signal
import subprocess
import tempfile
from os import makedirs
from os.path import expanduser, isdir, isfile, join
from threading import Thread
from time import sleep

import pexpect
from opentone import ToneGenerator
from pydub import AudioSegment
from responsive_voice import ResponsiveVoice

import baresipy.config
import baresipy.constants as const

logging.getLogger("urllib3.connectionpool").setLevel("WARN")
logging.getLogger("pydub.converter").setLevel("WARN")
logger: logging.Logger = logging.getLogger(__name__)


@dc.dataclass
class Identity:
    """An Account is the configuration for an identity used to make calls.

    If you want to suppress registration, add 'regint=0' to flags.

    The password is automatically migrated to the flags as 'auth_pass=<password>'
    """

    user: str
    password: str
    gateway: str
    flags: list[str]
    port: int = 5060

    @property
    def sip(self) -> str:
        """Returns the identity as a sip: address string with flags"""
        # Assign to self so this code doesn't end up putting auth_pass in the flags
        # repeatedly
        flags = copy.copy(self.flags)
        flags.append(f"auth_pass={self.password}")
        return f"sip:{self.user}@{self.gateway}:{self.port};{';'.join(flags)}"


class BareSIP(Thread):
    def __init__(
        self,
        identity: Identity,
        tts=None,
        block: bool = True,
        frame_rate: int = 8000,
        channels: int = 1,
        config_path: str | None = None,
        sounds_path: str | None = None,
    ):
        """BareSIP is a Thread that automatically runs baresip via pexpect

        The send_audio method is configured with:
        * frame_rate: Defaults to 8kHz, as that's the most compatible
        * channels: Defaults to mono audio, as that's the most compatible
        """
        config_path = config_path or join("~", ".baresipy")
        self.config_path = expanduser(config_path)
        if not isdir(self.config_path):
            makedirs(self.config_path)
        if isfile(join(self.config_path, "config")):
            with open(join(self.config_path, "config"), "r") as f:
                self.config = f.read()
            logger.info("config loaded from " + self.config_path + "/config")
            self.updated_config = False
        else:
            self.config = baresipy.config.DEFAULT
            self.updated_config = True

        self._original_config = str(self.config)

        if sounds_path is not None and "#audio_path" in self.config:
            self.updated_config = True
            if sounds_path is False:
                # sounds disabled
                self.config = self.config.replace(
                    "#audio_path		/usr/share/baresip", "audio_path		/dont/load"
                )
            elif isdir(sounds_path):
                self.config = self.config.replace(
                    "#audio_path		/usr/share/baresip", "audio_path		" + sounds_path
                )

        if self.updated_config:
            with open(join(self.config_path, "config.bak"), "w") as f:
                f.write(self._original_config)

            logger.info("saving config")
            with open(join(self.config_path, "config"), "w") as f:
                f.write(self.config)

        if tts:
            self.tts = tts
        else:
            self.tts = ResponsiveVoice(gender=ResponsiveVoice.MALE)

        self._identity = identity
        self._frame_rate = frame_rate
        self._channels = channels
        self._prev_output = ""
        self.running: bool = False
        self.ready: bool = False
        self.mic_muted: bool = False
        self.abort: bool = False
        self.current_call = None
        self._call_status: const.CallStatus = const.CallStatus.NONE
        self._previous_call_status: const.CallStatus = const.CallStatus.NONE
        self.audio = None
        self._ts = None
        self.baresip = pexpect.spawn("baresip -f " + self.config_path)
        super().__init__()
        self.start()
        if block:
            self.wait_until_ready()

    # properties
    @property
    def call_established(self) -> bool:
        return self.call_status == const.CallStatus.ESTABLISHED

    @property
    def call_status(self) -> const.CallStatus:
        return self._call_status

    # actions
    def do_command(self, action) -> None:
        if self.ready:
            action = str(action)
            self.baresip.sendline(action)
        else:
            logger.warning(action + " not executed!")
            logger.error("NOT READY! please wait")

    def create_user_agent(self) -> None:
        logger.info("Adding account to baresip: %s", self._identity.sip)
        self.baresip.sendline("/uanew " + self._identity.sip)

    def call(self, number: str) -> None:
        logger.info("Dialing: " + number)
        self.do_command("/dial " + number)

    def hang(self) -> None:
        if self.current_call:
            logger.info("Hanging: " + self.current_call)
            self.do_command("/hangup")
            self.current_call = None
            self._call_status = const.CallStatus.NONE
        else:
            logger.error("No active call to hang")

    def hold(self) -> None:
        if self.current_call:
            logger.info("Holding: " + self.current_call)
            self.do_command("/hold")
        else:
            logger.error("No active call to hold")

    def resume(self) -> None:
        if self.current_call:
            logger.info("Resuming: " + self.current_call)
            self.do_command("/resume")
        else:
            logger.error("No active call to resume")

    def mute_mic(self) -> None:
        if not self.call_established:
            logger.error("Cannot mute microphone while not in a call")
            return
        if not self.mic_muted:
            logger.info("Muting mic")
            self.do_command("/mute")
        else:
            logger.info("Mic already muted")

    def unmute_mic(self) -> None:
        if not self.call_established:
            logger.error("Cannot unmute microphone while not in a call")
            return
        if self.mic_muted:
            logger.info("Unmuting mic")
            self.do_command("/mute")
        else:
            logger.info("Mic already unmuted")

    def accept_call(self) -> None:
        self.do_command("/accept")
        self._handle_call_status(const.CallStatus.ESTABLISHED)

    def list_calls(self) -> None:
        self.do_command("/listcalls")

    def check_call_status(self) -> const.CallStatus:
        self.do_command("/callstat")
        sleep(0.1)
        return self.call_status

    def quit(self) -> None:
        if self.updated_config:
            logger.info("restoring original config")
            with open(join(self.config_path, "config"), "w") as f:
                f.write(self._original_config)
        logger.info("Exiting")
        if self.running:
            if self.current_call:
                self.hang()
            self.baresip.sendline("/quit")
        self.running = False
        self.current_call = None
        self._call_status = const.CallStatus.NONE
        self.abort = True
        self.baresip.close()
        self.baresip.kill(signal.SIGKILL)

    def send_dtmf(self, number: int) -> None:
        s_number = str(number)
        for n in s_number:
            if int(n) not in range(0, 9):
                logger.error("Invalid DTMF tone")
                return
        logger.info("Sending DTMF tones for " + s_number)
        dtmf = join(tempfile.gettempdir(), s_number + ".wav")
        ToneGenerator().dtmf_to_wave(number, dtmf)
        self.send_audio(dtmf)

    def speak(self, speech: str) -> None:
        if not self.call_established:
            logger.error("Speaking without an active call!")
        else:
            logger.info("Sending TTS for " + speech)
            self.send_audio(self.tts.get_mp3(speech))
            sleep(0.5)

    def send_audio(self, wav_file: str) -> None:
        if not self.call_established:
            logger.error("Can't send audio without an active call!")
            return
        wav_file, duration = self.convert_audio(
            wav_file, frame_rate=self._frame_rate, channels=self._channels
        )
        # send audio stream
        logger.info("transmitting audio")
        self.do_command("/ausrc aufile," + wav_file)
        # wait till playback ends
        sleep(duration - 0.5)
        # avoid baresip exiting
        self.do_command("/ausrc alsa,default")

    @staticmethod
    def convert_audio(
        input_file: str, frame_rate: int, channels: int, outfile=None
    ) -> tuple[str, int]:
        input_file = expanduser(input_file)
        sound = AudioSegment.from_file(input_file)
        sound += AudioSegment.silent(duration=500)
        # ensure minimum time
        # workaround baresip bug
        while sound.duration_seconds < 3:
            sound += AudioSegment.silent(duration=500)

        outfile = outfile or join(tempfile.gettempdir(), "pybaresip.wav")
        sound = sound.set_frame_rate(frame_rate)
        sound = sound.set_channels(channels)
        sound.export(outfile, format="wav")
        return outfile, sound.duration_seconds

    # this is played out loud over speakers
    def say(self, speech: str) -> None:
        if not self.call_established:
            logger.warning("Speaking without an active call!")
        self.tts.say(speech, blocking=True)

    def play(self, audio_file: str, blocking: bool = True) -> None:
        if not audio_file.endswith(".wav"):
            audio_file, duration = self.convert_audio(
                audio_file, frame_rate=self._frame_rate, channels=self._channels
            )
        self.audio = self._play_wav(audio_file, blocking=blocking)

    def stop_playing(self) -> None:
        if self.audio is not None:
            self.audio.kill()

    @staticmethod
    def _play_wav(wav_file, play_cmd="aplay %1", blocking=False):
        play_mp3_cmd = str(play_cmd).split(" ")
        for index, cmd in enumerate(play_mp3_cmd):
            if cmd == "%1":
                play_mp3_cmd[index] = wav_file
        if blocking:
            return subprocess.call(play_mp3_cmd)
        else:
            return subprocess.Popen(play_mp3_cmd)

    # events
    def handle_incoming_call(self, number: str) -> None:
        logger.info("Incoming call: " + number)
        if self.call_established:
            logger.info("already in a call, rejecting")
            sleep(0.1)
            self.do_command("b")
        else:
            logger.info("default behaviour, rejecting call")
            sleep(0.1)
            self.do_command("b")

    def handle_call_rejected(self, number: str) -> None:
        logger.info("Rejected incoming call: " + number)

    def handle_call_timestamp(self, timestr: str) -> None:
        logger.info("Call time: " + timestr)

    def _handle_call_status(self, status: const.CallStatus) -> None:
        self._previous_call_status = self._call_status
        self._call_status = status
        logger.debug(
            "Call status transition, %s to %s",
            self._previous_call_status.name,
            self._call_status.name,
        )
        self.handle_call_status_change(
            previous=self._previous_call_status, new=self._call_status
        )

    def handle_call_status_change(
        self, previous: const.CallStatus, new: const.CallStatus
    ) -> None:
        """Executed when a call status change is detected."""
        ...

    def _handle_call_start(self) -> None:
        """Internal method for handling a current call"""
        if self.current_call:
            logger.info("Calling: %s", self.current_call)
            self.handle_call_start(error=False)
        else:
            logger.error("In call startup, but self.current_call is None")
            self.handle_call_start(error=True)

    def handle_call_start(self, error: bool) -> None:
        """Executed when a call start is detected"""
        ...

    def _handle_call_ringing(self) -> None:
        """Internal method for handling a ringing current call"""
        if self.current_call:
            logger.info("Ringing: %s", self.current_call)
            self.handle_call_ringing(error=False)
        else:
            logger.error("In call ringing, but self.current_call is None")
            self.handle_call_ringing(error=True)

    def handle_call_ringing(self, error: bool) -> None:
        """Executed when a call ring is detected"""
        ...

    def handle_call_established(self) -> None:
        logger.info("Call established")

    def handle_call_ended(self, reason: str) -> None:
        logger.info("Call ended")
        logger.debug("Reason: " + reason)

    def _handle_no_accounts(self) -> None:
        logger.debug("No accounts in baresip, creating one")
        self.create_user_agent()

    def handle_login_success(self) -> None:
        logger.info("Logged in!")

    def handle_login_failure(self) -> None:
        logger.error("Log in failed!")
        self.quit()

    def handle_ready(self) -> None:
        logger.info("Ready for instructions")
        self.ready = True

    def handle_mic_muted(self) -> None:
        logger.info("Microphone muted")

    def handle_mic_unmuted(self) -> None:
        logger.info("Microphone unmuted")

    def handle_audio_stream_failure(self) -> None:
        logger.debug("Aborting call, maybe we reached voicemail?")
        self.hang()

    def handle_dtmf_received(self, char: str, duration: int) -> None:
        logger.info("Received DTMF symbol '{0}' duration={1}".format(char, duration))

    def handle_error(self, error: str) -> None:
        logger.error(error)
        if error == "failed to set audio-source (No such device)":
            self.handle_audio_stream_failure()

    # event loop
    def run(self) -> None:
        self.running = True
        while self.running:
            try:
                out = self.baresip.readline().decode("utf-8")

                if out != self._prev_output:
                    out = out.strip()
                    logger.debug("baresip> %s", out)
                    if "baresip is ready." in out:
                        self.handle_ready()
                    elif "account: No SIP accounts found" in out:
                        self._handle_no_accounts()
                    elif "All 1 useragent registered successfully!" in out:
                        self.ready = True
                        self.handle_login_success()
                    elif (
                        "ua: SIP register failed:" in out
                        or "401 Unauthorized" in out
                        or "Register: Destination address required" in out
                        or "Register: Connection timed out" in out
                    ):
                        self.handle_error(out)
                        self.handle_login_failure()
                    elif "Incoming call from: " in out:
                        num = (
                            out.split("Incoming call from: ")[1]
                            .split(" - (press 'a' to accept)")[0]
                            .strip()
                        )
                        self.current_call = num
                        self._handle_call_status(const.CallStatus.INCOMING)
                        self.handle_incoming_call(num)
                    elif "call: rejecting incoming call from " in out:
                        num = (
                            out.split("rejecting incoming call from ")[1]
                            .split(" ")[0]
                            .strip()
                        )
                        self.handle_call_rejected(num)
                    elif "call: SIP Progress: 180 Ringing" in out:
                        self._handle_call_ringing()
                        self._handle_call_status(const.CallStatus.RINGING)
                    elif "call: connecting to " in out:
                        n = out.split("call: connecting to '")[1].split("'")[0]
                        self.current_call = n
                        self._handle_call_start()
                        self._handle_call_status(const.CallStatus.OUTGOING)
                    elif "Call established:" in out:
                        self._handle_call_status(const.CallStatus.ESTABLISHED)
                        sleep(0.5)
                        self.handle_call_established()
                    elif "call: hold " in out:
                        n = out.split("call: hold ")[1]
                        self._handle_call_status(const.CallStatus.ON_HOLD)
                    elif "Call with " in out and "terminated (duration: " in out:
                        duration = out.split("terminated (duration: ")[1][:-1]
                        self._handle_call_status(const.CallStatus.DISCONNECTED)
                        self.handle_call_timestamp(duration)
                        self.mic_muted = False
                    elif "call muted" in out:
                        self.mic_muted = True
                        self.handle_mic_muted()
                    elif "call un-muted" in out:
                        self.mic_muted = False
                        self.handle_mic_unmuted()
                    elif "session closed:" in out:
                        reason = out.split("session closed:")[1].strip()
                        self._handle_call_status(const.CallStatus.DISCONNECTED)
                        self.handle_call_ended(reason)
                        self.mic_muted = False
                    elif "(no active calls)" in out:
                        self._handle_call_status(const.CallStatus.DISCONNECTED)
                    elif "===== Call debug " in out:
                        status = out.split("(")[1].split(")")[0]
                        logger.warning(
                            "baresip produced status '%s', mapping to CallStatus.UNKNOWN",
                            status,
                        )
                        self._handle_call_status(const.CallStatus.UKNOWN)
                    elif "--- List of active calls (1): ---" in self._prev_output:
                        if "ESTABLISHED" in out and self.current_call in out:
                            ts = (
                                out.split("ESTABLISHED")[0].split("[line 1]")[1].strip()
                            )
                            if ts != self._ts:
                                self._ts = ts
                                self.handle_call_timestamp(ts)
                    elif "failed to set audio-source (No such device)" in out:
                        error = "failed to set audio-source (No such device)"
                        self.handle_error(error)
                    elif "terminated by signal" in out or "ua: stop all" in out:
                        self.running = False
                    elif "received DTMF:" in out:
                        match = re.search(
                            r"received DTMF: '(.)' \(duration=(\d+)\)", out
                        )
                        if match:
                            self.handle_dtmf_received(
                                match.group(1), int(match.group(2))
                            )
                    self._prev_output = out
            except pexpect.exceptions.EOF:
                # baresip exited
                self.running = False
            except pexpect.exceptions.TIMEOUT:
                # nothing happened for a while
                pass
            except KeyboardInterrupt:
                self.running = False

        self.quit()

    def wait_until_ready(self) -> None:
        while not self.ready:
            sleep(0.1)
            if self.abort:
                return
