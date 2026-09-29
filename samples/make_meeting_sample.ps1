# Makes samples/team-meeting.wav: a short, made-up planning meeting spoken by the Windows
# text-to-speech voices (System.Speech, offline). The script below was written for this
# repository, so the recording is free to use; no real people or recordings are involved.
#
#   powershell -ExecutionPolicy Bypass -File samples/make_meeting_sample.ps1
#
# Then compress it for the repo (any ffmpeg; the worker image has one):
#   ffmpeg -i samples/team-meeting.wav -ac 1 -ar 16000 -b:a 48k samples/team-meeting.mp3
#
# Needs the "Microsoft Zira/David/Hazel Desktop" voices that ship with Windows 10/11.

Add-Type -AssemblyName System.Speech

$lines = @(
    @("Microsoft Zira Desktop", "Good morning everyone. This is the weekly planning call for the FlowForge release. We have three things to cover today: the launch date, the audio feature, and the support rota."),
    @("Microsoft David Desktop", "On the launch date: the build is stable, and the load test passed on Thursday. I suggest we ship version two point one on Tuesday, October the sixth."),
    @("Microsoft Hazel Desktop", "I agree with Tuesday. The only risk is the documentation. The meeting notes template still needs screenshots."),
    @("Microsoft Zira Desktop", "Then let's decide. We ship on Tuesday, October the sixth. Elena, can you finish the documentation and the screenshots by Friday?"),
    @("Microsoft Hazel Desktop", "Yes. I'll have the docs done by Friday."),
    @("Microsoft David Desktop", "For the audio feature, transcription works with Groq, but long recordings need more testing. I'll test a one hour recording and report back by Monday."),
    @("Microsoft Zira Desktop", "Great. Second decision: Groq stays the default speech provider, and local transcription stays optional."),
    @("Microsoft Hazel Desktop", "One more thing. Customers asked for Telegram alerts. Marcus, could you add that to the release notes?"),
    @("Microsoft David Desktop", "Sure. I'll update the release notes today."),
    @("Microsoft Zira Desktop", "Perfect. To sum up: we ship on Tuesday, Groq stays the default, Elena owns the docs, and Marcus owns the testing and the release notes. Thanks, everyone.")
)

$prompt = New-Object System.Speech.Synthesis.PromptBuilder
foreach ($line in $lines) {
    $prompt.StartVoice($line[0])
    $prompt.AppendText($line[1])
    $prompt.EndVoice()
    $prompt.AppendBreak([TimeSpan]::FromMilliseconds(700))
}

$out = Join-Path $PSScriptRoot "team-meeting.wav"
$format = New-Object System.Speech.AudioFormat.SpeechAudioFormatInfo(22050, [System.Speech.AudioFormat.AudioBitsPerSample]::Sixteen, [System.Speech.AudioFormat.AudioChannel]::Mono)
$synth = New-Object System.Speech.Synthesis.SpeechSynthesizer
$synth.SetOutputToWaveFile($out, $format)
$synth.Rate = 0
$synth.Speak($prompt)
$synth.Dispose()
Write-Output "wrote $out"
