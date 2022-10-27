import tensorflow as tf
import numpy as np
import matplotlib.pyplot as plt

from sklearn.preprocessing import OneHotEncoder as OHE
from sklearn.metrics import accuracy_score

gpus = tf.config.list_physical_devices('GPU')
for gpu in gpus:
    tf.config.experimental.set_memory_growth(gpu, True)

class HINCEPTION:

    def __init__(self, output_directory, length_TS, n_classes, batch_size=64, max_cf_length=6,
                 n_filters=32, use_residual=True, use_bottleneck=True, depth=6, epochs=1500):

        '''
        Class defining the Hybrid Inception network (H-Inception)

        Args:

            output_directory : the directory to save all the trained models and metric plots (str)
            length_TS: Length of the input Time Series (int)
            n_filters : number of filters to learn in the learnable part of the model (int)
            n_classes : number of classes in the dataset (int)
            batch_size : number of samples in each batch (int)
            epochs : number of epochs to train the model (int)
            max_cf_length : maximum number of custom filters lengths to use per type (inc, dec, peak) (int)
            use_residual : To use residual connection or not (bool)
            use_bottleneck : To use bottleneck or not (bool)
            depth : number of inception modules (int)
        '''

        self.output_directory = output_directory

        self.n_filters = n_filters
        self.use_residual = use_residual
        self.use_bottleneck = use_bottleneck

        self.depth = depth
        self.kernel_sizes = [40, 20, 10] # As used in the InceptionTime paper done by H. Ismail Fawaz et al.
        
        self.batch_size = batch_size
        self.bottleneck_size = self.n_filters
        self.epochs = epochs

        self.max_cf_length = max_cf_length

        self.length_TS = length_TS
        self.n_classes = n_classes

        self.increasing_trend_kernels = [2**i for i in range(1,self.max_cf_length + 1)]
        self.decreasing_trend_kernels = [2**i for i in range(1,self.max_cf_length + 1)]
        self.peak_kernels = [2**i for i in range(2,self.max_cf_length + 1)]

        self.build_model()

    def hybrid_layer(self,input_tensor,input_channels,kernel_sizes=[2,4,8,16,32,64]):
    

        '''
        Function to create the hybrid layer consisting of non trainable Conv1D layers with custom filters.

        Args:

            input_tensor: input tensor
            input_channels : number of input channels, 1 in case of UCR Archive
        '''

        conv_list = []

        # for increasing detection filters

        for kernel_size in kernel_sizes:

            filter_ = np.ones(shape=(kernel_size,input_channels,1)) # define the filter weights with the shape corresponding the Conv1D layer in keras (kernel_size, input_channels, output_channels)
            indices_ = np.arange(kernel_size)

            filter_[indices_ % 2 == 0] *= -1 # formula of increasing detection filter

            # Create a Conv1D layer with non trainable option and no biases and set the filter weights that were calculated in the line above as the initialization

            conv = tf.keras.layers.Conv1D(filters=1,kernel_size=kernel_size,padding='same',
                                          use_bias=False,kernel_initializer=tf.keras.initializers.Constant(filter_),
                                          trainable=False,name='hybrid-increasse-'+str(self.keep_track)+'-'+str(kernel_size))(input_tensor)

            conv_list.append(conv) # add the conv layer to the list

            self.keep_track += 1

        # for decreasing detection filters
        
        for kernel_size in kernel_sizes:

            filter_ = np.ones(shape=(kernel_size,input_channels,1)) # define the filter weights with the shape corresponding the Conv1D layer in keras (kernel_size, input_channels, output_channels)
            indices_ = np.arange(kernel_size)

            filter_[indices_ % 2 > 0] *= -1 # formula of decreasing detection filter

            # Create a Conv1D layer with non trainable option and no biases and set the filter weights that were calculated in the line above as the initialization

            conv = tf.keras.layers.Conv1D(filters=1,kernel_size=kernel_size,padding='same',
                                          use_bias=False,kernel_initializer=tf.keras.initializers.Constant(filter_),
                                          trainable=False,name='hybrid-decrease-'+str(self.keep_track)+'-'+str(kernel_size))(input_tensor)
            
            conv_list.append(conv) # add the conv layer to the list

            self.keep_track += 1

        # for peak detection filters
        
        for kernel_size in kernel_sizes[1:]:

            filter_ = np.zeros(shape=(kernel_size + kernel_size // 2,input_channels,1))

            xmesh = np.linspace(start=0,stop=1,num=kernel_size//4+1)[1:].reshape((-1,1,1))

            # see utils.custom_filters.py to understand the formulas below

            filter_left = xmesh**2
            filter_right = filter_left[::-1]

            filter_[0:kernel_size // 4] = -filter_left
            filter_[kernel_size // 4:kernel_size // 2] = -filter_right
            filter_[kernel_size // 2:3 * kernel_size // 4] = 2 * filter_left
            filter_[3 * kernel_size // 4:kernel_size] = 2 * filter_right
            filter_[kernel_size:5 * kernel_size // 4] = -filter_left
            filter_[5 * kernel_size // 4:] = -filter_right
            
            # Create a Conv1D layer with non trainable option and no biases and set the filter weights that were calculated in the line above as the initialization

            conv = tf.keras.layers.Conv1D(filters=1,kernel_size=kernel_size+kernel_size//2,padding='same',
                                          use_bias=False,kernel_initializer=tf.keras.initializers.Constant(filter_),
                                          trainable=False,name='hybrid-peeks-'+str(self.keep_track)+'-'+str(kernel_size))(input_tensor)

            conv_list.append(conv) # add the conv layer to the list

            self.keep_track += 1

        
        hybrid_layer = tf.keras.layers.Concatenate(axis=2)(conv_list) # concantenate all convolution layers
        hybrid_layer = tf.keras.layers.Activation(activation='relu')(hybrid_layer) # apply activation ReLU

        return hybrid_layer

    def _inception_module(self, input_tensor, stride=1, activation='linear',use_hybrid_layer=False):

        # Add bottleneck if input is multivariate in the middle or beginning of network

        if self.use_bottleneck and int(input_tensor.shape[-1]) > 1:
            input_inception = tf.keras.layers.Conv1D(filters=self.bottleneck_size, kernel_size=1,
                                                  padding='same', activation=activation, use_bias=False)(input_tensor)
        else:
            input_inception = input_tensor

        conv_list = []

        # create the inception convolutions 

        for kernel_size in self.kernel_sizes:
            conv_list.append(tf.keras.layers.Conv1D(filters=self.n_filters, kernel_size=kernel_size,
                                                 strides=stride, padding='same', activation=activation, use_bias=False)(
                input_inception))

        # add a max pooling procedure

        max_pool_1 = tf.keras.layers.MaxPool1D(pool_size=3, strides=stride, padding='same')(input_tensor)

        # add bottleneck after the max pooling

        conv_6 = tf.keras.layers.Conv1D(filters=self.n_filters, kernel_size=1,
                                     padding='same', activation=activation, use_bias=False)(max_pool_1)

        conv_list.append(conv_6)

        # add hybrid layer

        if use_hybrid_layer:

            self.hybrid = self.hybrid_layer(input_tensor=input_tensor,input_channels=input_tensor.shape[-1])
            conv_list.append(self.hybrid)

        # concatenate everything and add batchnorm with relu acrivation

        x = tf.keras.layers.Concatenate(axis=2)(conv_list)
        x = tf.keras.layers.BatchNormalization()(x)
        x = tf.keras.layers.Activation(activation='relu')(x)

        return x

    def _shortcut_layer(self, input_tensor, out_tensor):

        # Function to add residual connection between input and output tensors

        shortcut_y = tf.keras.layers.Conv1D(filters=int(out_tensor.shape[-1]), kernel_size=1,
                                         padding='same', use_bias=False)(input_tensor)
        shortcut_y = tf.keras.layers.BatchNormalization()(shortcut_y)

        x = tf.keras.layers.Add()([shortcut_y, out_tensor])
        x = tf.keras.layers.Activation('relu')(x)

        return x

    def build_model(self):

        self.keep_track = 0

        input_shape = (self.length_TS, 1)

        input_layer = tf.keras.layers.Input(input_shape)

        x = input_layer
        input_res = input_layer

        # Create the H-Inception network

        for d in range(self.depth):

            # add custom filters hybrid layer only on the first layer

            if d == 0:
                x = self._inception_module(input_tensor=x,use_hybrid_layer=True)
            else:
                x = self._inception_module(input_tensor=x)

            # each three inception modules add a residual connection

            if self.use_residual and d % 3 == 2:
                x = self._shortcut_layer(input_res, x)
                input_res = x

        gap_layer = tf.keras.layers.GlobalAveragePooling1D()(x)

        output_layer = tf.keras.layers.Dense(self.n_classes, activation='softmax')(gap_layer)

        self.model = tf.keras.models.Model(inputs=input_layer, outputs=output_layer)

        self.model.compile(loss='categorical_crossentropy', optimizer=tf.keras.optimizers.Adam(),
                      metrics=['accuracy'])

        reduce_lr = tf.keras.callbacks.ReduceLROnPlateau(monitor='loss', factor=0.5, patience=50,
                                                      min_lr=0.0001)

        file_path = self.output_directory + 'best_model.hdf5'

        model_checkpoint = tf.keras.callbacks.ModelCheckpoint(filepath=file_path, monitor='loss',
                                                           save_best_only=True)

        self.callbacks = [reduce_lr, model_checkpoint]

    def fit(self, xtrain, ytrain, xval=None, yval=None, plot_test=False):

        # xval and yval are only used to visualize the losses and accuracies and not for training.
        
        n = int(xtrain.shape[0])

        mini_batch_size = min(self.batch_size,n // 10)

        ohe = OHE(sparse=False)
        ytrain = np.expand_dims(ytrain,axis=1)
        ytrain = ohe.fit_transform(ytrain)

        if plot_test:

            ohe = OHE(sparse=False)
            yval = np.expand_dims(yval, axis=1)
            yval = ohe.fit_transform(yval)

        if plot_test:

            hist = self.model.fit(xtrain, ytrain, batch_size=mini_batch_size, epochs=self.epochs,
                                  callbacks=self.callbacks, validation_data=(xval, yval))

        else:

            hist = self.model.fit(xtrain, ytrain, batch_size=mini_batch_size, epochs=self.epochs,
                                  callbacks=self.callbacks)

        plt.figure(figsize=(20,10))

        plt.plot(hist.history['loss'], lw=3, color='blue', label="Training Loss")

        if plot_test:
            plt.plot(hist.history['val_loss'], lw=3, color='red', label="Validation Loss")

        plt.savefig(self.output_directory+'loss.pdf')
        plt.cla()

        plt.plot(hist.history['accuracy'], lw=3, color='blue', label="Training Accuracy")

        if plot_test:
            plt.plot(hist.history['val_accuracy'], lw=3, color='red', label="Validation Accuracy")
        
        plt.savefig(self.output_directory + 'accuracy.pdf')

        plt.cla()
        plt.clf()

    def predict(self, xtest, ytest):

        model = tf.keras.models.load_model(self.output_directory+'best_model.hdf5',compile=False)

        ypred = model.predict(xtest)
        ypred_argmax = np.argmax(ypred,axis=1)

        tf.keras.backend.clear_session()

        return np.asarray(ypred), accuracy_score(y_true=ytest,y_pred=ypred_argmax,normalize=True)