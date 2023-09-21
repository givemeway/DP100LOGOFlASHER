import os,logging

if not os.path.exists("logs"):
    os.makedirs("logs",exist_ok=True)
try:
    if os.path.exists('logs/FileTransfer.log'):
        os.remove('logs/FileTransfer.log')
    if os.path.exists('logs/SendCommand.log'):
        os.remove('logs/SendCommand.log')
    if os.path.exists('logs/PrintCommand.log'):
        os.remove('logs/PrintCommand.log')
    if os.path.exists('logs/error.log'):
        os.remove('logs/error.log')
    if os.path.exists('logs/imageConvert.log'):
        os.remove('logs/imageConvert.log')
except Exception as e:
    print('Unable to delete',e)

    #### Loggers###############
fileTransfer = logging.getLogger('FileTransfer')
imageConvert = logging.getLogger('ImageConvert')
sendCommand = logging.getLogger('SendCommand')
printCommand = logging.getLogger('PrintCommand')
error = logging.getLogger('error')
    ############# SET LOG LEVEL ##################
fileTransfer.setLevel(logging.DEBUG)
imageConvert.setLevel(logging.DEBUG)
sendCommand.setLevel(logging.DEBUG)
printCommand.setLevel(logging.DEBUG)
error.setLevel(logging.ERROR)
    ############  FORMATTER #######################
formatter = logging.Formatter('%(asctime)s:%(levelname)s:%(name)s:%(message)s')

    ############# FILE handlers ########################
fileTransfer_handler = logging.FileHandler('logs/FileTransfer.log')
imageConvert_handler = logging.FileHandler('logs/imageConvert.log')
sendCommand_handler = logging.FileHandler('logs/SendCommand.log')
printCommand_handler = logging.FileHandler('logs/PrintCommand.log')
error_handler = logging.FileHandler('logs/error.log')
    ################## set formatter ################################
fileTransfer_handler.setFormatter(formatter)
imageConvert_handler.setFormatter(formatter)
sendCommand_handler.setFormatter(formatter)
printCommand_handler.setFormatter(formatter)
error_handler.setFormatter(formatter)

    #################### Add handler ############################
fileTransfer.addHandler(fileTransfer_handler)
imageConvert.addHandler(imageConvert_handler)
sendCommand.addHandler(sendCommand_handler)
printCommand.addHandler(printCommand_handler)
error.addHandler(error_handler)

if __name__=="__main__":
    pass