#!/usr/bin/python
# Copyright (c) 2013 Datera, Inc. All rights reserved
# Datera, Inc. Confidential and Propriatory

import sys
import os
import fcntl
import subprocess
import shelve
import operator
import time
import string
import random

from optparse import OptionParser, OptionGroup

# script directory and path
DIR=os.path.dirname(__file__)       # path to script directory

# global info file settings:
IP_FILE = "/etc/vms/ips"
IP_FILE_LOCK = IP_FILE+".lock"      # File we use to lock access to VM info list or dict
IP_FILE_DICT = IP_FILE+".dict"      # Shelf file used to store vm information as python objects
MAX_VMS = 100                       # Max number of VMs. Currently this is pretty arbitrary.

DEFAULT_MEMORY = "1G"               # how much memory to give the vms
DEFAULT_TMP = "/tmp"                # default directory for tmps
DEFAULT_TIMEOUT = 10*60             # timeout: 10 mins

USER_NETWORKING = True

# colors:
GREEN='\033[32m'
CYAN='\033[36m'
NC='\033[0m'

DEVNULL = open(os.devnull, "wb")


""" 
Functions of opening shelf to lock while
reading and writing to shelf file
"""
def openLockedDb():
   lockFD = os.open(IP_FILE_LOCK,os.O_RDONLY)
   fcntl.flock(lockFD, fcntl.LOCK_EX)
   db = shelve.open(IP_FILE_DICT)
   db.lockFD = lockFD
   return db

def closeLockedDb(db):
   db.close()
   fcntl.flock(db.lockFD, fcntl.LOCK_UN)
   os.close(db.lockFD)

"""
Print Error:
"""
def die(msg, exitStatus=1):
   print >> sys.stderr, "vm-script error: "+msg
   sys.exit(exitStatus)

def warning(msg):
   print >> sys.stderr, "vm script warning: "+msg

class Domain:
   """Represents a single virtual machine"""
   def __init__ (self, vm_id, ip, mac):
      self.vm_id = vm_id         # id number for this vm
      self.ip = ip               # ip address
      self.mac = mac             # mac addresss
      self.owner = None          # who started this machine
      self.pid = None            # qemu process ID
      self.name = "vm"+str(vm_id)   # name of the vm, can be custom
      self.base = None           # base image if this isn't persistant
      self.kernel = None         # kernel image booted with
      self.tmpdir  = None        # the location of dir to store scratch drives

   def exists(self):
      """Does the VM this represents actually exists?"""
      if subprocess.call("ps aux | grep "+str(self.tmpdir)+" | grep "+str(self.pid)+
            " | grep -v grep", shell=True, stdout=DEVNULL):
         return False
      return True

   def kill(self):
      """This kills the VM"""
      if not self.pid:
         print "Vm "+self.name+"never started. Releasing domain"
         return True

      if not self.exists():
         print "Vm "+self.name+" terminated. Releasing domain"
         return True

      if subprocess.call("kill "+str(self.pid), shell=True):
         print "Error killing vm: "+self.name
         return False
      
      print "Vm "+self.name+" terminated. Releasing domain"
      return True

def randStr(size=12, chars=string.letters + string.digits):
   return "".join(random.choice(chars) for x in range(size))

def cmd_start():
   """Start new virtual machines"""

   opt=OptionParser("usage: %prog vm start [number]",
         description="""Boot start virtual machines""")
   opt.add_option("-i", "--image", dest="image",
         help="boot image of the virtual machine")
   opt.add_option("-k", "--kernel", dest="kernel",
         help="kernel qemu should use to boot")
   opt.add_option("-c", "--cdrom-source", dest="cdrom",
         help="source to made into a cdrom")
   opt.add_option("-s", "--scratch-dev ", dest="scratch",
         help="list of scratch device sizes. ex: 256M,2G")
   opt.add_option("-t", "--tmpfs-dir", dest="tmpfs", default=DEFAULT_TMP,
         help="location of the tmpfs directory. Only used for scratch devs")
   opt.add_option("-m", "--memory", dest="mem", default=DEFAULT_MEMORY,
         help="amount of memory to be allocated per VM")
   opt.add_option("-p", "--persistant", dest="persistant", action="store_true",
         help="turns off snapshot, so writeback happens to disk instead of tmp")
   opt.add_option("--id", dest="indexfile", help="file to write vm number to")
   opt.add_option("--vde", dest="vde", action="store_true", 
         help="use vde networking instead of user mode. This requires a vde"+
              "switch to be set up, and an dns setup to listen")
   (options, args) = opt.parse_args()

   if not options.image:
      die ("Requires an image to boot")
   if not os.path.isfile(options.image):
      die("Cannot find base image file: "+options.image)
   #if not options.kernel:
   #   die("Requires a kernel to boot")
   if options.kernel and not os.path.isfile(options.kernel):
      die("Cannot find kernel file: "+options.kernel)

   # get user if SUDO
   try:
      user = os.environ["SUDO_USER"]
   except KeyError:
      user = os.environ["USER"]

   #load database:
   d = openLockedDb()
   free_vms = d["free_vms"]

   # check to make sure we have enough domains
   if len(free_vms) == 0:
      d.close()
      dropLock(lock)
      die("Not enough domains to start a vms")
   
   # get domain, mark for setup
   vm = free_vms.pop()
   vm.state = "setup"
   vm.owner = user 

   # save and free up database while launching vms
   d["free_vms"] = free_vms
   closeLockedDb(d)

   try:
      # generate tmp dir
      vm.tmpdir = options.tmpfs+"/"+randStr()+"-vm"+str(vm.vm_id)+"-"+user
      try: 
         os.mkdir(vm.tmpdir, 0700)
      except OSError:
         die ("Failed to create tmpdir: "+vm.tmpdir)
      vm.tmpdir += "/"

      # setup run CD:
      if not options.cdrom:
         iso=None
      elif os.path.isdir(options.cdrom): 
         iso = vm.tmpdir + "run.iso"
         if subprocess.call(["genisoimage", "-quiet", "-R", "-input-charset", 
                            "utf-8", "-o", iso, options.cdrom]):
            die("Could not create cdrom from dir: "+options.cdrom)
      else:
         iso=options.cdrom

      # generates scratch drives:
      drives = []
      if options.scratch:
         sizes = options.scratch.split(",")
         i = 1;
         for size in sizes:
            name = str(vm.tmpdir)+"disk-"+str(i)
            i += 1
            if subprocess.call(["fallocate", "-l", size, name]):
               die ("Error creating a "+size+" scratch drive")
            drives.append(name)

      if options.indexfile:
         f=open(options.indexfile, "w")
         f.write(str(vm.vm_id))
         f.close()
   
      # build qemu command and start
      cmd = ["qemu-system-x86_64"]
      cmd.extend(["-machine", "accel=kvm"])
      cmd.extend(["-pidfile", vm.tmpdir + "pid"])
      cmd.extend(["-m", options.mem])
      #cmd.extend(["-vnc", "0.0.0.0:"+str(17100+vm.vm_id)])
      if options.kernel:
         cmd.extend(["-kernel", options.kernel])
         cmd.extend(["-append", "root=/dev/sda rw console=ttyS0,115200 kgdboc=ttyS2,115200"])
      cmd.extend(["-nographic"])
      cmd.extend(["-s"])
      cmd.extend(["-serial", "stdio"])
      cmd.extend(["-serial", "mon:unix:" + vm.tmpdir + "con,server,nowait"])
      cmd.extend(["-serial",     "unix:" + vm.tmpdir + "gdb,server,nowait"])
      cmd.extend(["-monitor",    "unix:" + vm.tmpdir + "mon,server,nowait"])
      if options.persistant:
         cmd.extend(["-drive", "format=raw,file="+options.image])
      else:
         cmd.extend(["-drive", "format=raw,snapshot=on,file="+options.image])
      for drive in drives:  # add scratch drives
         cmd.extend(["-drive", "if=virtio,format=raw,cache=writeback,file="+drive])
      if iso:              # add cdrom
         cmd.extend(["-cdrom", iso])
      if options.vde:   # enable vde networking
         cmd.extend(["-netdev", "vde,id=vde_net,sock=/tmp/vde.ctl"])
         cmd.extend(["-device", "virtio-net-pci,netdev=vde_net,mac="+vm.mac])
         vm.vde = True
      else:             # user networking instead
         cmd.extend(["-net", "nic,model=virtio"])
         cmd.extend(["-net", "user,hostfwd=tcp:127.0.0.1:" + str(vm.vm_id + 9000) + "-:22"])
         vm.vde = False
      
      p = subprocess.Popen(cmd, shell=False)

      vm.pid = p.pid
      vm.status="running"
      print GREEN+"Vm ID: "+str(vm.vm_id)+" IP: "+vm.ip+" MAC: "+vm.mac+NC

   except Exception as e: # so we don't lose vm if exception occurs. Prolly could be better made
      d = openLockedDb()
      free_vms = d["free_vms"]
      free_vms.append(vm)
      d["free_vms"] = sorted(free_vms, key=operator.attrgetter("vm_id"), reverse=True) 
      closeLockedDb(d)
      
      exc_type, exc_obj, exc_tb = sys.exc_info()
      fname = os.path.split(exc_tb.tb_frame.f_code.co_filename)[1]
      die("Unexpected Exception: "+str(exc_type)+", File: "+str(fname)+", Line: "+str(exc_tb.tb_lineno))

   # resave vms with process and pid information
   d = openLockedDb()
   running_vms=d["running_vms"]
   running_vms.append(vm)
   d["running_vms"] = running_vms
   closeLockedDb(d)

   try:
      p.wait()
   except KeyboardInterrupt:
      warning("KeyBoard interrupt raised")

   __stop(vm.vm_id) 
 
def cmd_ssh():
   """ssh into a virtual machine"""

   if len(sys.argv)<=1:
      die("Please provide a vm id to SSH to")
   id=int(sys.argv[1])

   d = openLockedDb()
   running_vms=d["running_vms"]
   vm = next((vm for vm in running_vms if vm.vm_id == id), None)
   closeLockedDb(d)

   if vm == None:
      die("No vm running with id: "+str(id))

   keyfile=os.path.dirname(os.path.realpath(__file__)) + "/id_dsa"
   if vm.vde:
      os.execlp("ssh", "ssh", "-o", "StrictHostKeyChecking=no", "-o",
                "UserKnownHostsFile=/dev/null", "-i", keyfile, "root@"+vm.ip)
   else:
      os.execlp("ssh", "ssh", "-o", "NoHostAuthenticationForLocalhost=yes",
            "-i", keyfile, "-p", str(9000 + id), "root@localhost")

def cmd_stop():
   """Stop virtual machines"""

   opt=OptionParser("usage: %prog vm stop [id]",
         description="""Stop virtual machines""")
   (options, args) = opt.parse_args()

   if len(args)==0:
      die("Please provide a vm id to stop")
   
   __stop(int(args[0]))

def cmd_gdb():
   if len(sys.argv)<=2:
      die("Please provide a session ID to SSH to and a vmlinux file")

   id=int(sys.argv[1])

   d = openLockedDb()
   running_vms = d["running_vms"]
   vm = next((vm for vm in running_vms if vm.vm_id == id), None)

   # make sure we actually found some
   if not vm:
      closeLockedDb(d)
      die("vm " + str(id) + " not running")

   tmpdir = vm.tmpdir
   closeLockedDb(d)

   os.execlp("gdb", "gdb",
           "-ex", "set remote interrupt-on-connect",
           "-ex", "target remote | socat UNIX-CONNECT:" + tmpdir + "/gdb -",
           sys.argv[2])

def cmd_mon():
   if len(sys.argv)<=1:
      die("Please provide a session ID")

   id=int(sys.argv[1])

   d = openLockedDb()
   running_vms = d["running_vms"]
   vm = next((vm for vm in running_vms if vm.vm_id == id), None)

   # make sure we actually found some
   if not vm:
      closeLockedDb(d)
      die("vm " + str(id) + " not running")

   tmpdir = vm.tmpdir
   closeLockedDb(d)

   os.execlp("minicom", "minicom",
           "-D", "unix#" + tmpdir + "/mon")


def __stop(stop_id):
   #get vm to stop
   d = openLockedDb()
   running_vms = d["running_vms"]
   vm = next((vm for vm in running_vms if vm.vm_id == stop_id), None)

   # make sure we actually found some
   if not vm:
      closeLockedDb(d)
      die("No Running vms with id "+str(stop_id))

   # check if they exist and kill if needed
   print "Stopping vm "+str(vm.vm_id)
      
   # kill the vm:
   if not vm.kill():
      # if it can't killed? what now?
      if vm.exists():
         closeLockedDb(d)
         die("Couldn't kill: "+vm.name)

   if vm.tmpdir:
      os.system("rm -rf " + vm.tmpdir);

   vm.state = "stopped"
   vm.owner = None          
   vm.pid = None                          
   vm.name = "vm"+str(vm.vm_id)
   vm.base = None
   vm.kernel = None
   vm.tmpdir = None
   vm.vde = None

   # save changes
   running_vms.remove(vm)
   free_vms = d["free_vms"]
   free_vms.append(vm)
   d["free_vms"] =  sorted(free_vms, key=operator.attrgetter("vm_id"), reverse=True)
   d["running_vms"]=running_vms
   closeLockedDb(d)

def cmd_list():
   """List out running VMs"""
   opt=OptionParser("usage: %prog vm list [session_id]",
         description="""List out running VMs""")
   (options, args) = opt.parse_args()
      
   # get running vms:
   d = openLockedDb()
   running_vms = d["running_vms"]
   closeLockedDb(d)

   running_vms = sorted(running_vms, key=operator.attrgetter("owner", "vm_id"))
   
   print "Domain\tIP\t\tOwner\tPID\tDead\tSPECIAL"
   print "------\t------------\t-----\t-----\t----\t-------"

   for vm in running_vms:
      mia=" " if vm.exists() == True else "X"
      line=(str(vm.name)+"\t"+str(vm.ip)+"\t"+str(vm.owner)+"\t"+
            str(vm.pid)+"\t"+mia+"\t")
      print line


def cmd_genips():
   """
   Generates a new set of new IP Macaddress combinations for
   and puts them in the proper locations:
   Vm.ips-dict - shelve dictionary used by python
   Vm.dhcp - contains dhcp entries for dhcpd

   For use when setting up networking on a new machine, or if 
   any of the config files get screwed up.
   """

   opt=OptionParser("usage: %prog vm genips",
         description="""Generate a new IP/MAC file""")
   (options, args) = opt.parse_args()

   if os.geteuid() != 0: 
      die("You need root permissions to do this, laterz!")

   check = raw_input("This will overwrite any current vm list files. Are you sure? (y): ")
   if check != "y":
      sys.exit(0)

   # make directory
   if not os.path.isdir("/etc/vms"):
      try:
         os.mkdir("/etc/vms")
      except OSError:
         die("Failed to create /etc/vms")

   # generate database files:
   name = "ips"
   dhcp = open("Vm.dhcp", "w")

   d = shelve.open(IP_FILE_DICT)
   d["sess_id"] = 1000
   free_vms = []
   lock = open(IP_FILE_LOCK, "w")
   lock.write("used for locking access to vm database file")
   
   for i in range (0, 100):
      #generate IP file
      vm_num = str(i).zfill(2)
      name = "vm"+vm_num
      ip = "172.20.0."+str(i+100)
      mac = "DE:AD:BE:EF:01:"+vm_num

      #generate DHCP entries
      dhcp.write("\thost "+name+" {\n")
      dhcp.write("\t\thardware ethernet "+mac+";\n")
      dhcp.write("\t\tfixed-address "+ip+";\n")
      dhcp.write("\t}\n\n")

      #generate ips dict
      vm = Domain(i, ip, mac)
      free_vms.insert(0, vm)

   dhcp.close()
   d["free_vms"] = free_vms
   d["running_vms"] = []
   d.close()
   lock.close()

   os.chmod(IP_FILE_DICT, 0666)
   os.chmod(IP_FILE_LOCK, 0666)

   print "Setup completed"

def cmd_help():
   print """List of valid commands:
   start    starts a vm
   stop     stop a vm by name
   list     list all running vms
   genips   generate a fresh vm database
   """

# main:
if len(sys.argv) == 1:
   cmd_help()
   die("Please give a command")

if sys.argv[1] == "-h":
   cmd_help()
   sys.exit(0)

# try looking up givn command in global namespace:
try:
   func = "cmd_"+sys.argv[1]
   func = getattr(sys.modules[__name__], func)
except AttributeError:
   cmd_help()
   die(sys.argv[1]+" is not a valid command")

# check for vm.dict setup:
if func != cmd_genips:
   if not os.path.isfile(IP_FILE_DICT):
      die("Vm list file not found. Please run genips")
   if not os.path.isfile(IP_FILE_LOCK):
      die("Lock file for vm list access not found. Did you run genips?")

sys.argv.pop(1)
func()
