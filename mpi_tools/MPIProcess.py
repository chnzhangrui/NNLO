### MPIWorker and MPIMaster classes 

import os,sys
import numpy as np
from mpi4py import MPI

from mpi_tools.Utils import Error, weights_from_shapes, shapes_from_weights

### Classes ###

class MPIProcess(object):
    """Base class for processes that communicate with one another via MPI.  

       Attributes:
           parent_comm: MPI intracommunicator used to communicate with this process's parent
           parent_rank (integer): rank of this node's parent in parent_comm
           rank (integer): rank of this node in parent_comm
           model: Keras model to train
           model_arch: json string giving model architecture information
           algo: Algo object defining how to optimize model weights
           weights_shapes: list of tuples indicating the shape of each layer of the model
           weights: list of numpy arrays storing the last weights received from the parent
           update: latest update obtained from training
           data: Data object used to generate training or validation data
           time_step: for keeping track of time
    """

    def __init__(self, parent_comm, parent_rank=None, data=None):
        """If the rank of the parent is given, initialize this process and immediately start 
            training. If no parent is indicated, model information should be set manually
            with set_model_info() and training should be launched with train().
            
            Parameters:
              parent_comm: MPI intracommunicator used to communicate with parent
              parent_rank (integer): rank of this node's parent in parent_comm
              data: Data object used to generate training or validation data
        """
        self.parent_comm = parent_comm 
        self.parent_rank = parent_rank
        self.rank = parent_comm.Get_rank()
        self.data = data
        self.model = None
        self.model_arch = None
        self.algo = None
        self.weights_shapes = None
        self.weights = None
        self.update = None
        self.time_step = 0

        if self.parent_rank is not None:
            self.bcast_model_info( self.parent_comm )
            self.set_model_info( model_arch=self.model_arch, weights=self.weights )
            self.train()
        else:
            warning = ("MPIProcess {0} created with no parent rank. "
                        "Please initialize manually")
            print warning.format(self.rank)

    def set_model_info(self, model_arch=None, algo=None, weights=None):
        """Sets NN architecture, training algorithm, and weights.
            Any parameter not provided is skipped."""
        if model_arch is not None:
            self.model_arch = model_arch
            from keras.models import model_from_json
            self.model = model_from_json( self.model_arch )
        if algo is not None:
            self.algo = algo
        if weights is not None:
            self.weights = weights
            self.weights_shapes = shapes_from_weights( self.weights )
            self.model.set_weights(self.weights)
            self.update = weights_from_shapes( self.weights_shapes )

    def check_sanity(self):
        """Throws an exception if any model attribute has not been set yet."""
        for par in ['model','model_arch','algo','weights_shapes','weights']:
            if not hasattr(self, par) or getattr(self, par) is None:
                raise Error("%s not found!  Process %d does not seem to be set up correctly." % (par,self.rank))

    def train(self):
        """To be implemented in derived classes"""
        raise NotImplementedError

    def compile_model(self):
        """Compile the model. Note that the compilation settings
            are relevant only for Workers because the Master updates
            its weights using an mpi_learn optimizer."""
        print "Process %d compiling model" % self.rank
        self.algo.compile_model( self.model )

    def print_metrics(self, metrics):
        """Display metrics computed during training or validation"""
        names = self.model.metrics_names
        if len(names) == 1:
            print "%s: %.3f" % (names[0],metrics)
        else:
            for i,m in enumerate(names):
                print "%s: %.3f" % (m,metrics[i]),
            print ""

    def do_send_sequence(self):
        """Actions to take when sending an update to parent:
            -Send the update (if the parent accepts it)
            -Sync time and model weights with parent"""
        self.send_update(check_permission=True)
        self.sync_with_master()

    def sync_with_master(self):
        self.time_step = self.recv_time_step()
        self.recv_weights()
        self.algo.set_worker_model_weights( self.model, self.weights )

    ### MPI-related functions below ###

    # This dict associates message strings with integers to be passed as MPI tags.
    tag_lookup = {
            'any':          MPI.ANY_SOURCE,
            'train':          0,
            'exit':           1,
            'begin_weights':  2,
            'begin_update'  : 3,
            'time':           4,
            'bool':           5,
            'model_arch':     10,
            'algo':           11,
            'weights_shapes': 12,
            'weights':        13,
            'update':         13,
            }
    # This dict is for reverse tag lookups.
    inv_tag_lookup = { value:key for key,value in tag_lookup.iteritems() }

    def lookup_mpi_tag( self, name, inv=False ):
        """Searches for the indicated name in the tag lookup table and returns it if found.
            Params:
              name: item to be looked up
              inv: boolean that is True if an inverse lookup should be performed (int --> string)"""
        if inv:
            lookup = self.inv_tag_lookup
        else:
            lookup = self.tag_lookup
        try:
            return lookup[name]
        except KeyError:
            print "Error: not found in tag dictionary: %s -- returning None" % name
            return None

    def recv(self, obj=None, tag=MPI.ANY_TAG, source=None, buffer=False, status=None, comm=None):
        """Wrapper around MPI.recv/Recv. Returns the received object.
            Params:
              obj: variable into which the received object should be placed
              tag: string indicating which MPI tag should be received
              source: integer rank of the message source.  Defaults to self.parent_rank
              buffer: True if the received object should be sent as a single-segment buffer
                (e.g. for numpy arrays) using MPI.Recv rather than MPI.recv
              status: MPI status object that is filled with received status information
              comm: MPI communicator to use.  Defaults to self.parent_comm"""
        if comm is None:
            comm = self.parent_comm
        if source is None:
            if self.parent_rank is None:
                raise Error("Attempting to receive %s from parent, but parent rank is None" % tag)
            source = self.parent_rank 
        tag_num = self.lookup_mpi_tag(tag)
        if buffer:
            comm.Recv( obj, source=source, tag=tag_num, status=status )
            return obj
        else:
            obj = comm.recv( source=source, tag=tag_num, status=status )
            return obj

    def send(self, obj, tag, dest=None, buffer=False, comm=None):
        """Wrapper around MPI.send/Send.  Params:
             obj: object to send
             tag: string indicating which MPI tag to send
             dest: integer rank of the message destination.  Defaults to self.parent_rank
             buffer: True if the object should be sent as a single-segment buffer
                (e.g. for numpy arrays) using MPI.Send rather than MPI.send
             comm: MPI communicator to use.  Defaults to self.parent_comm"""
        if comm is None:
            comm = self.parent_comm
        if dest is None:
            if self.parent_rank is None:
                raise Error("Attempting to send %s to parent, but parent rank is None" % tag)
            dest = self.parent_rank
        tag_num = self.lookup_mpi_tag(tag)
        if buffer:
            comm.Send( obj, dest=dest, tag=tag_num )
        else:
            comm.send( obj, dest=dest, tag=tag_num )

    def bcast(self, obj, root=0, buffer=False, comm=None):
        """Wrapper around MPI.bcast/Bcast.  Returns the broadcasted object.
            Params: 
              obj: object to broadcast
              root: rank of the node to broadcast from
              buffer: True if the object should be sent as a single-segment buffer
                (e.g. for numpy arrays) using MPI.Bcast rather than MPI.bcast
              comm: MPI communicator to use.  Defaults to self.parent_comm"""
        if comm is None:
            comm = self.parent_comm
        if buffer:
            comm.Bcast( obj, root=root )
        else:
            obj = comm.bcast( obj, root=root )
            return obj

    def send_exit_to_parent(self):
        """Send exit tag to parent process, if parent process exists"""
        if self.parent_rank is not None:
            self.send( None, 'exit' )

    def send_arrays(self, obj, expect_tag, tag, comm=None, dest=None, check_permission=False):
        """Send a list of numpy arrays to the process specified by comm (MPI communicator) 
            and dest (rank).  We first send expect_tag to tell the dest process that we 
            are sending several buffer objects, then send the objects layer by layer.
            Optionally check first to see if the update will be accepted by the master"""
        self.send( None, expect_tag, comm=comm, dest=dest )
        if check_permission:
            # To check permission we send the update's time stamp to the master.
            # Then we wait to receive the decision yes/no.
            self.send_time_step( comm=comm, dest=dest )
            decision = self.recv_bool( comm=comm, source=dest )
            if not decision: return
        for w in obj:
            self.send( w, tag, comm=comm, dest=dest, buffer=True )

    def send_weights(self, comm=None, dest=None, check_permission=False):
        """Send NN weights to the process specified by comm (MPI communicator) and dest (rank).
            Before sending the weights we first send the tag 'begin_weights'."""
        self.send_arrays( self.weights, expect_tag='begin_weights', tag='weights', 
                comm=comm, dest=dest, check_permission=check_permission )

    def send_update(self, comm=None, dest=None, check_permission=False):
        """Send update to the process specified by comm (MPI communicator) and dest (rank).
            Before sending the update we first send the tag 'begin_update'"""
        self.send_arrays( self.update, expect_tag='begin_update', tag='update', 
                comm=comm, dest=dest, check_permission=check_permission )

    def send_time_step(self, comm=None, dest=None):
        """Send the current time step"""
        self.send( obj=self.time_step, tag='time', dest=dest, comm=comm )

    def send_bool(self, obj, comm=None, dest=None):
        self.send( obj=obj, tag='bool', dest=dest, comm=comm )

    def recv_arrays(self, obj, tag, comm=None, source=None, add_to_existing=False):
        """Receive a list of numpy arrays from the process specified by comm (MPI communicator) 
            and dest (rank).
              obj: list of destination arrays 
              tag: MPI tag accompanying the message
              add_to_existing: if true, add to existing object instead of replacing"""
        if add_to_existing:
            tmp = weights_from_shapes( [ w.shape for w in obj ] )
            self.recv_arrays( tmp, tag=tag, comm=comm, source=source )
            for i in range(len(obj)):
                obj[i] += tmp[i]
            return
        for w in obj:
            self.recv( w, tag, comm=comm, source=source, buffer=True )

    def recv_weights(self, comm=None, source=None, add_to_existing=False):
        """Receive NN weights layer by layer from the process specified by comm and source"""
        self.recv_arrays( self.weights, tag='weights', comm=comm, source=source,
                add_to_existing=add_to_existing )

    def recv_update(self, comm=None, source=None, add_to_existing=False):
        """Receive an update layer by layer from the process specified by comm and source.
            Add it to the current update if add_to_existing is True, 
            otherwise overwrite the current update"""
        self.recv_arrays( self.update, tag='update', comm=comm, source=source,
                add_to_existing=add_to_existing )

    def recv_time_step(self, comm=None, source=None):
        """Receive the current time step"""
        return self.recv( tag='time', comm=comm, source=source )

    def recv_bool(self, comm=None, source=None):
        return self.recv( tag='bool', comm=comm, source=source )

    def bcast_weights(self, comm, root=0):
        """Broadcast weights layer by layer on communicator comm from the indicated root rank"""
        for w in self.weights:
            self.bcast( w, comm=comm, root=root, buffer=True )

    def bcast_model_info(self, comm, root=0):
        """Broadcast model architecture, optimization algorithm, and weights shape
            using communicator comm and the indicated root rank"""
        for tag in ['model_arch','algo','weights_shapes']:
            setattr( self, tag, self.bcast( getattr(self, tag), comm=comm, root=root ) )
        if self.weights is None:
            self.weights = weights_from_shapes( self.weights_shapes )
        self.bcast_weights( comm, root )


class MPIWorker(MPIProcess):
    """This class trains its NN model and exchanges weight updates with its parent.
        Attributes:
          num_epochs: integer giving the number of epochs to train for
    """

    def __init__(self, data, parent_comm, parent_rank=None, num_epochs=1):
        """Raises an exception if no parent rank is provided. Sets the number of epochs 
            using the argument provided, then calls the parent constructor"""
        if parent_rank is None:
            raise Error("MPIWorker initialized without parent rank")
        self.num_epochs = num_epochs
        info = "Creating MPIWorker with rank {0} and parent rank {1} on a communicator of size {2}" 
        print info.format(parent_comm.Get_rank(),parent_rank, parent_comm.Get_size())
        super(MPIWorker, self).__init__( parent_comm, parent_rank, data=data )

    def train(self):
        """Compile the model, then wait for the signal to train. Then train for num_epochs epochs.
            In each step, train on one batch of input data, then send the update to the master
            and wait to receive a new set of weights.  When done, send 'exit' signal to parent.
        """
        self.check_sanity()
        self.compile_model()
        self.await_signal_from_parent()
        for epoch in range(self.num_epochs):
            print "MPIWorker %d beginning epoch %d" % (self.rank, epoch)
            for batch in self.data.generate_data():
                self.train_on_batch(batch)
                self.compute_update()
                self.do_send_sequence()
        print "MPIWorker %d signing off" % self.rank
        self.send_exit_to_parent()

    def train_on_batch(self, batch):
        """Train on a single batch"""
        train_loss = self.model.train_on_batch( batch[0], batch[1] )
        print "Worker %d metrics:"%self.rank,
        self.print_metrics(train_loss)

    def compute_update(self):
        """Compute the update from the new and old sets of model weights"""
        self.update = self.algo.compute_update( self.weights, self.model.get_weights() )

    def await_signal_from_parent(self):
        """Wait for 'train' signal from parent process"""
        self.recv( tag='train' )

class MPIMaster(MPIProcess):
    """This class sends model information to its worker processes and updates its model weights
        according to updates or weights received from the workers.
        
        Attributes:
          child_comm: MPI intracommunicator used to communicate with child processes
          has_parent: boolean indicating if this process has a parent process
          num_workers: integer giving the number of workers that work for this master
          best_val_loss: best validation loss computed so far during training
          running_workers: number of workers not yet done training
          waiting_workers_list: list of workers that sent updates and are now waiting
          num_sync_workers: number of worker updates to receive before performing an update
          update_tag: MPI tag to expect when workers send updates
    """

    def __init__(self, parent_comm, parent_rank=None, child_comm=None, data=None,
            num_sync_workers=1):
        """Parameters:
              child_comm: MPI communicator used to contact children"""
        if child_comm is None:
            raise Error("MPIMaster initialized without child communicator")
        self.child_comm = child_comm
        self.has_parent = False
        if parent_rank is not None:
            self.has_parent = True
        self.best_val_loss = None
        self.num_workers = child_comm.Get_size() - 1 #all processes but one are workers
        self.num_sync_workers = num_sync_workers
        info = ("Creating MPIMaster with rank {0} and parent rank {1}. "
                "(Communicator size {2}, Child communicator size {3})")
        print "Will wait for updates from %d workers before synchronizing" % self.num_sync_workers
        print info.format(parent_comm.Get_rank(),parent_rank,parent_comm.Get_size(), 
                child_comm.Get_size())
        super(MPIMaster, self).__init__( parent_comm, parent_rank, data=data )

    def decide_whether_to_sync(self):
        """Check whether enough workers have sent updates"""
        return ( len(self.waiting_workers_list) >= self.num_sync_workers )

    def is_synchronous(self):
        return self.num_sync_workers > 1

    def accept_update(self):
        """Returns true if the master should accept the latest worker's update, false otherwise"""
        return (not self.is_synchronous()) or self.algo.staleness == 0
        
    def sync_children(self):
        """Update model weights and signal all waiting workers to work again.
            Send our update to our parent, if we have one"""
        while self.waiting_workers_list:
            child = self.waiting_workers_list.pop()
            self.sync_child(child)

    def sync_child(self, child):
        self.send_time_step( dest=child, comm=self.child_comm )
        self.send_weights( dest=child, comm=self.child_comm )

    def sync_parent(self):
        if self.has_parent:
            self.do_send_sequence()
        else:
            self.time_step += 1 

    def do_update_sequence(self, source):
        """Update procedure:
         -Compute the staleness of the update and decide whether to accept it.
         -If we accept, we signal the worker and wait to receive the update.
         -After receiving the update, we determine whether to sync with the workers.
         -Finally we run validation if we have completed one epoch's worth of updates."""
        self.algo.staleness = self.time_step - self.recv_time_step( 
                source=source, comm=self.child_comm )
        accepted = self.accept_update()
        self.send_bool( accepted, dest=source, comm=self.child_comm )
        if accepted:
            self.recv_update( source=source, comm=self.child_comm, 
                    add_to_existing=self.is_synchronous() )
            self.waiting_workers_list.append(source)
            if self.decide_whether_to_sync():
                if self.algo.send_before_apply:
                    self.sync_parent()
                    self.sync_children()
                    self.apply_update()
                else:
                    self.apply_update()
                    self.sync_parent()
                    self.sync_children()
                self.update = weights_from_shapes( self.weights_shapes ) #reset update variable
            if self.time_step % self.algo.validate_every == 0 and self.time_step > 0:
                self.validate()
        else:
            self.sync_child(source)

    def process_message(self, status):
        """Extracts message source and tag from the MPI status object and processes the message. 
            Returns the tag of the message received.
            Possible messages are:
            -begin_update: worker is ready to send a new update
            -exit: worker is done training and will shut down
        """
        source = status.Get_source()
        tag = self.lookup_mpi_tag( status.Get_tag(), inv=True )
        if tag == 'begin_update':
            self.do_update_sequence(source)
        elif tag == 'exit':
            self.running_workers -= 1 
            self.num_sync_workers -= 1
        else:
            raise ValueError("Tag %s not recognized" % tag)
        return tag

    def train(self):
        """Broadcasts model information to children and signals them to start training.
            Receive messages from workers and processes each message until training is done.
            When finished, signal the parent process that training is complete.
        """
        self.check_sanity()
        self.bcast_model_info( comm=self.child_comm )
        self.compile_model()
        self.signal_children()

        status = MPI.Status()
        self.running_workers = self.num_workers
        self.waiting_workers_list = []
        
        while self.running_workers > 0:
            self.recv_any_from_child(status)
            self.process_message( status )
        print "MPIMaster %d done training" % self.rank
        self.validate()
        self.send_exit_to_parent()

    def validate(self, save_if_best=True):
        """Compute the loss on the validation data.
            If save_if_best is true, save the model if the validation loss is the 
            smallest so far."""
        if self.has_parent:
            return
        self.model.set_weights(self.weights)

        n_batches = 0
        val_metrics = [ 0.0 for i in range( len(self.model.metrics) ) ]
        for batch in self.data.generate_data():
            n_batches += 1
            val_metrics = np.add( val_metrics, self.model.test_on_batch(*batch) )
        val_metrics = np.divide( val_metrics, n_batches )
        print "Validation metrics:",
        self.print_metrics(val_metrics)
        if save_if_best:
            self.save_model_if_best(val_metrics)

    def apply_update(self):
        """Updates weights according to update received from worker process"""
        self.weights = self.algo.apply_update( self.weights, self.update )

    def save_model_if_best(self, val_metrics):
        """If the validation loss is the lowest on record, save the model.
            The output file name is mpi_learn_model.h5"""
        if hasattr( val_metrics, '__getitem__'):
            val_loss = val_metrics[0]
        else:
            val_loss = val_metrics

        if self.best_val_loss is None or val_loss < self.best_val_loss:
            self.best_val_loss = val_loss
            print "Saving model to mpi_learn_model.h5"
            self.model.save('mpi_learn_model.h5')

    ### MPI-related functions below

    def signal_children(self):
        """Sends each child a message telling them to start training"""
        for child in range(1, self.child_comm.Get_size()):
            self.send( obj=None, tag='train', dest=child, comm=self.child_comm )

    def recv_any_from_child(self,status):
        """Receives any message from any child.  Returns the provided status object,
            populated with information about received message"""
        self.recv( tag='any', source=MPI.ANY_SOURCE, status=status, comm=self.child_comm )
        return status
