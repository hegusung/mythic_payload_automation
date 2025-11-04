# mythic_payload_automation

This project aims to automate the creation of infection chains within Mythic. Containers used by infection chains are referenced here: https://github.com/hegusung/MythicInfectionPayloads

## Examples

2 config files are provided in the examples folder to generate the following chains:

- Scenario 1 : zip -> LNK -> powershell download and IEX -> powershell executing a shellcode -> apollo shellcode
- Scenario 2 : clickfix -> regsvr32 on a remote SCT -> JScript download exe and execute it -> apollo exe


## Install

First copy the config file 

```sh
cp mythic.yml.sample mythic.yml
```

Change the values inside to reflect your Mythic setup (Mythic password, Listeners URLs)

Then install the requirements:

```sh
pip3 install -r requirements.txt
```

## Running the script

Simply execute it via :

```sh
python3 main.py --config examples/
```
