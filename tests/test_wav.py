import requests

url = "https://api.siliconflow.cn/v1/audio/speech"

payload = {
    "model": "FunAudioLLM/CosyVoice2-0.5B",
    "input": "Can you say it with a happy emotion? <|endofprompt|>I'm so happy, Spring Festival is coming!",
    "voice": "FunAudioLLM/CosyVoice2-0.5B:anna",
    "response_format": "wav",
    "sample_rate": 32000,
    "stream": True,
    "speed": 1,
    "gain": 0
}
headers = {
    "Authorization": "Bearer sk-nuquonnskijxjttcuubbnqfoohieknygtthpsstwrzzuqdqn",
    "Content-Type": "application/json"
}

response = requests.request("POST", url, json=payload, headers=headers)

print(response.text)
