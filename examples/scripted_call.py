from time import sleep

from baresipy import BareSIP

gateway = "your_sip.gateway.net"
user = "your_phone"
pswd = "your_password"
debug = False

b = BareSIP(user, pswd, gateway, debug=debug)

to = "jarbas_laptop@sipx.mattkeys.net"
speech = "this is jarbas personal assistant speaking. this was a test"
audio = "examples/acdc.mp3"


b.call(to)

while b.running:
    sleep(0.5)
    if b.call_established:
        b.send_dtmf("123")

        b.speak(speech)
        b.speak("Goodbye")
        # b.send_audio(audio)
        b.hang()
        b.quit()
